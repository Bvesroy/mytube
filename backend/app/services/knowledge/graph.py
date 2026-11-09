"""Personal knowledge graph (Feature 1).

Nodes are concepts/topics, channels and (optionally) videos and research
collections. Edges are *only* derived from stored evidence:

- `discusses`      video_concepts rows written by extraction
- `specializes`    channel-level aggregation of its videos' concept links
- `prerequisite`   concept_relations written from explicit statements
- `related`        concept_relations written from co-occurrence in one video
- `similar`        embedding cosine above a threshold (only when vectors exist)
- `belongs_to`     course/collection membership

Every edge carries `confidence` and `provenance`; the API never invents an edge
because two labels look alike.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Channel,
    Collection,
    CollectionVideo,
    Concept,
    ConceptRelation,
    Course,
    CourseVideo,
    EmbeddingVector,
    Video,
    VideoConcept,
    WatchHistory,
)
from app.services.ai.embeddings import configured_engine, cosine, unpack_vector


def _prov(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def _watched_ids(session: Session) -> set[int]:
    return {
        int(row[0])
        for row in session.execute(select(WatchHistory.video_id).where(WatchHistory.completed == 1)).all()
    }


def build_graph(
    session: Session,
    *,
    include_videos: bool = False,
    include_collections: bool = False,
    channel_yt_id: str | None = None,
    concept_slug: str | None = None,
    watched: bool | None = None,
    min_confidence: float = 0.0,
    limit: int = 300,
) -> dict[str, Any]:
    watched_set = _watched_ids(session)
    channel_filter_id: int | None = None
    if channel_yt_id:
        ch = session.scalar(select(Channel).where(Channel.yt_id == channel_yt_id))
        if ch is None:
            return {"nodes": [], "edges": [], "stats": {"concepts": 0, "videos": 0, "channels": 0}}
        channel_filter_id = ch.id

    video_q = select(Video).where(Video.is_available == 1)
    if channel_filter_id:
        video_q = video_q.where(Video.channel_id == channel_filter_id)
    videos = {v.id: v for v in session.scalars(video_q).all()}
    if watched is True:
        videos = {vid: v for vid, v in videos.items() if vid in watched_set}
    elif watched is False:
        videos = {vid: v for vid, v in videos.items() if vid not in watched_set}

    links = session.execute(
        select(VideoConcept, Concept)
        .join(Concept, Concept.id == VideoConcept.concept_id)
        .where(VideoConcept.video_id.in_(list(videos.keys())) if videos else VideoConcept.video_id < 0)
    ).all() if videos else []
    if concept_slug:
        links = [(link, concept) for link, concept in links if concept.slug == concept_slug]

    by_concept: dict[int, list[VideoConcept]] = defaultdict(list)
    concept_rows: dict[int, Concept] = {}
    for link, concept in links:
        by_concept[concept.id].append(link)
        concept_rows[concept.id] = concept

    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    def add_node(node_id: str, payload: dict[str, Any]) -> None:
        nodes.setdefault(node_id, {"id": node_id, **payload})

    def add_edge(
        source: str,
        target: str,
        relation: str,
        *,
        confidence: float,
        provenance: dict[str, Any],
        explanation: str = "",
    ) -> None:
        if confidence < min_confidence:
            return
        edge_id = f"{source}|{relation}|{target}"
        if edge_id in edges:
            return
        edges[edge_id] = {
            "id": edge_id,
            "source": source,
            "target": target,
            "relation": relation,
            "confidence": round(float(confidence), 3),
            "provenance": provenance,
            "explanation": explanation or _explain(relation, provenance),
        }

    concept_items = sorted(
        by_concept.items(), key=lambda kv: (-len(kv[1]), concept_rows[kv[0]].name.lower())
    )[: max(1, limit)]
    for concept_id, concept_links in concept_items:
        concept = concept_rows[concept_id]
        video_ids = [link.video_id for link in concept_links]
        watched_count = sum(1 for vid in video_ids if vid in watched_set)
        channels = {videos[vid].channel_id for vid in video_ids if vid in videos}
        add_node(
            f"concept:{concept.slug}",
            {
                "type": "concept",
                "kind": concept.kind,
                "label": concept.name,
                "slug": concept.slug,
                "description": concept.description,
                "video_count": len(video_ids),
                "channel_count": len(channels),
                "watched_count": watched_count,
                "watched_ratio": round(watched_count / len(video_ids), 3) if video_ids else 0.0,
                "confidence": round(
                    sum(float(link.confidence or 0) for link in concept_links) / len(concept_links), 3
                ),
            },
        )

    concepts_by_id = {c.id: c for c in concept_rows.values()}
    for relation in session.scalars(select(ConceptRelation)).all():
        if relation.src_concept_id not in concepts_by_id or relation.dst_concept_id not in concepts_by_id:
            continue
        src = concepts_by_id[relation.src_concept_id]
        dst = concepts_by_id[relation.dst_concept_id]
        add_edge(
            f"concept:{src.slug}",
            f"concept:{dst.slug}",
            relation.relation,
            confidence=float(relation.confidence or 0),
            provenance=_prov(relation.provenance_json),
        )

    selected_video_ids: list[int] = []
    if include_videos:
        ranked = sorted(
            ((vid, sum(float(l.weight or 0) for l in by_concept[cid])) for cid, links in by_concept.items() for vid in {l.video_id for l in links}),
            key=lambda item: -item[1],
        )
        selected_video_ids = [vid for vid, _ in ranked[: max(1, min(200, limit))]]
        for vid in selected_video_ids:
            video = videos.get(vid)
            if video is None:
                continue
            add_node(
                f"video:{video.yt_id}",
                {
                    "type": "video",
                    "label": video.title,
                    "duration_s": video.duration_s,
                    "channel_id": video.channel_id,
                    "watched": vid in watched_set,
                    "published_at": video.published_at,
                },
            )
            if video.channel_id:
                ch = session.get(Channel, video.channel_id)
                if ch is not None:
                    add_node(
                        f"channel:{ch.yt_id}",
                        {"type": "channel", "label": ch.name, "channel_id": ch.id},
                    )
                    add_edge(
                        f"video:{video.yt_id}",
                        f"channel:{ch.yt_id}",
                        "published_by",
                        confidence=1.0,
                        provenance={"method": "foreign_key", "table": "videos"},
                    )
            for link in session.scalars(
                select(VideoConcept).where(VideoConcept.video_id == vid)
            ).all():
                concept = concepts_by_id.get(link.concept_id)
                if concept is None:
                    continue
                add_edge(
                    f"video:{video.yt_id}",
                    f"concept:{concept.slug}",
                    "discusses",
                    confidence=float(link.confidence or 0),
                    provenance={"method": link.provenance or "extracted", "weight": link.weight},
                )

    # Channel specialisation: aggregated only, with the count shown in provenance.
    channel_concepts: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for concept_id, concept_links in by_concept.items():
        for link in concept_links:
            video = videos.get(link.video_id)
            if video is None or not video.channel_id:
                continue
            channel_concepts[video.channel_id][concept_id].append(float(link.confidence or 0))
    for channel_id, per_concept in channel_concepts.items():
        ch = session.get(Channel, channel_id)
        if ch is None:
            continue
        add_node(f"channel:{ch.yt_id}", {"type": "channel", "label": ch.name, "channel_id": ch.id})
        for concept_id, confidences in per_concept.items():
            concept = concepts_by_id.get(concept_id)
            if concept is None:
                continue
            add_edge(
                f"channel:{ch.yt_id}",
                f"concept:{concept.slug}",
                "specializes",
                confidence=round(sum(confidences) / len(confidences), 3),
                provenance={
                    "method": "aggregate",
                    "videos": len(confidences),
                    "channel_id": ch.yt_id,
                },
            )

    # Video-video similarity only from real stored vectors.
    if include_videos and len(selected_video_ids) > 1:
        engine = configured_engine()
        rows = session.execute(
            select(EmbeddingVector).where(
                EmbeddingVector.owner_type == "video",
                EmbeddingVector.model_id == engine.model_id,
                EmbeddingVector.owner_id.in_(selected_video_ids),
            )
        ).scalars().all()
        vecs = [(row.owner_id, unpack_vector(row.vector, row.dim)) for row in rows]
        for i, (a_id, a_vec) in enumerate(vecs):
            for b_id, b_vec in vecs[i + 1 :]:
                score = cosine(a_vec, b_vec)
                if score < 0.25:
                    continue
                a_video = videos.get(a_id)
                b_video = videos.get(b_id)
                if not a_video or not b_video:
                    continue
                add_edge(
                    f"video:{a_video.yt_id}",
                    f"video:{b_video.yt_id}",
                    "similar",
                    confidence=score,
                    provenance={"method": "embedding_cosine", "model_id": engine.model_id},
                )

    if include_collections:
        courses = session.scalars(select(Course).where(Course.archived == 0)).all()
        for course in courses:
            add_node(f"collection:{course.id}", {"type": "collection", "label": course.name, "kind": "course"})
            for link in session.scalars(
                select(CourseVideo).where(CourseVideo.course_id == course.id)
            ).all():
                video = session.get(Video, link.video_id)
                if video is None:
                    continue
                add_node(f"video:{video.yt_id}", {"type": "video", "label": video.title})
                add_edge(
                    f"video:{video.yt_id}",
                    f"collection:{course.id}",
                    "belongs_to",
                    confidence=1.0,
                    provenance={"method": "course_membership", "course_id": course.id},
                )
        for collection in session.scalars(select(Collection)).all():
            add_node(
                f"collection:{collection.id}",
                {"type": "collection", "label": collection.name, "kind": collection.kind},
            )
            for link in session.scalars(
                select(CollectionVideo).where(CollectionVideo.collection_id == collection.id)
            ).all():
                video = session.get(Video, link.video_id)
                if video is None:
                    continue
                add_node(f"video:{video.yt_id}", {"type": "video", "label": video.title})
                add_edge(
                    f"video:{video.yt_id}",
                    f"collection:{collection.id}",
                    "belongs_to",
                    confidence=1.0,
                    provenance={"method": "collection_membership"},
                )

    degree: dict[str, int] = defaultdict(int)
    for edge in edges.values():
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1
    for node in nodes.values():
        node["degree"] = degree.get(node["id"], 0)

    # Only return edges whose endpoints exist.
    final_edges = [e for e in edges.values() if e["source"] in nodes and e["target"] in nodes]
    counts = defaultdict(int)
    for node in nodes.values():
        counts[node["type"]] += 1
    return {
        "nodes": list(nodes.values()),
        "edges": final_edges,
        "stats": {
            "concepts": counts.get("concept", 0),
            "videos": counts.get("video", 0),
            "channels": counts.get("channel", 0),
            "collections": counts.get("collection", 0),
            "edges": len(final_edges),
        },
        "filters": {
            "include_videos": include_videos,
            "channel": channel_yt_id,
            "concept": concept_slug,
            "watched": watched,
            "min_confidence": min_confidence,
        },
        "provenance_note": (
            "Edges are derived from stored extraction, explicit statements, co-occurrence "
            "or stored embeddings. Confidence and method are shown per edge."
        ),
    }


def _explain(relation: str, provenance: dict[str, Any]) -> str:
    method = provenance.get("method", "stored evidence")
    return {
        "discusses": f"Extraction linked this video to the concept ({method}).",
        "specializes": "The channel publishes multiple videos about this concept.",
        "prerequisite": "An explicit prerequisite statement in the source transcript.",
        "related": "The concepts co-occur in the same source video.",
        "similar": "Stored embeddings are close (cosine similarity).",
        "published_by": "The video belongs to this channel.",
        "belongs_to": "Collection membership.",
    }.get(relation, f"Derived from {method}.")


def graph_node(session: Session, node_id: str, *, limit: int = 30) -> dict[str, Any] | None:
    if node_id.startswith("concept:"):
        slug = node_id.split(":", 1)[1]
        concept = session.scalar(select(Concept).where(Concept.slug == slug))
        if concept is None:
            return None
        links = session.execute(
            select(VideoConcept, Video)
            .join(Video, Video.id == VideoConcept.video_id)
            .where(VideoConcept.concept_id == concept.id)
            .order_by(VideoConcept.weight.desc())
            .limit(limit)
        ).all()
        relations = session.execute(
            select(ConceptRelation, Concept)
            .join(Concept, Concept.id == ConceptRelation.dst_concept_id)
            .where(ConceptRelation.src_concept_id == concept.id)
        ).all()
        return {
            "id": node_id,
            "type": "concept",
            "label": concept.name,
            "description": concept.description,
            "kind": concept.kind,
            "videos": [
                {
                    "id": video.yt_id,
                    "title": video.title,
                    "channel_id": video.channel_id,
                    "confidence": round(float(link.confidence or 0), 3),
                }
                for link, video in links
            ],
            "relations": [
                {
                    "concept": other.name,
                    "slug": other.slug,
                    "relation": relation.relation,
                    "confidence": round(float(relation.confidence or 0), 3),
                    "provenance": _prov(relation.provenance_json),
                }
                for relation, other in relations
            ],
        }
    if node_id.startswith("video:"):
        yt_id = node_id.split(":", 1)[1]
        from app.services.knowledge.extraction import knowledge_summary

        return knowledge_summary(session, yt_id)
    return None


def search_nodes(session: Session, query: str, *, limit: int = 30) -> list[dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        return []
    rows = session.execute(
        select(Concept)
        .where(func.lower(Concept.name).contains(q))
        .order_by(Concept.name)
        .limit(limit)
    ).scalars().all()
    return [
        {"id": f"concept:{c.slug}", "type": "concept", "label": c.name, "slug": c.slug, "kind": c.kind}
        for c in rows
    ]
