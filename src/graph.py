"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`.

    Both sides are normalized so case, the "Tội " prefix and accents are ignored; the ORIGINAL
    spelling from `known` is returned. Exact match wins; otherwise the closest normalized candidate
    with difflib ratio >= 0.8. Nothing close enough -> None (linking wrongly is worse than not linking).
    """
    if not name or not known:
        return None
    target = normalize(name)
    if not target:
        return None
    by_normalized = {normalize(candidate): candidate for candidate in known if candidate}
    if target in by_normalized:
        return by_normalized[target]
    close = difflib.get_close_matches(target, list(by_normalized), n=1, cutoff=0.8)
    return by_normalized[close[0]] if close else None

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# CUSTOM ONTOLOGY (bonus) — stable identity keys, canonical synonyms, amount thresholds
#
# Differences vs the suggested ontology, each solving a concrete problem:
#   1. Case      keyed by `uid = doc_id#i`  -> one article never splits/collides on an LLM-chosen name
#   2. Person    keyed by normalized `key`   -> "Lê Minh Thành" written differently still merges
#   3. Substance canonical via synonyms      -> "ma túy kẹo"/"thuốc lắc"/"MDMA" become one node
#   4. Amount thresholds parsed from the law -> Clause-[:MENTIONS {amount_min_g, amount_max_g}]
#      and Case-[:INVOLVES {amount, amount_grams}] let Cypher pick the exact khoản for Q5.
# ----------------------------------------------------------------------------------------------

SUBSTANCE_SYNONYMS = {
    "ma túy đá": "Methamphetamine", "đá": "Methamphetamine", "methamphetamine": "Methamphetamine",
    "thuốc lắc": "MDMA", "ma túy kẹo": "MDMA", "kẹo": "MDMA", "ecstasy": "MDMA",
    "heroin": "Heroine", "bạch phiến": "Heroine",
    "ketamin": "Ketamine",
    "cocain": "Cocaine",
    "cỏ": "cần sa", "cần sa (cannabis)": "cần sa",
    "nhựa thuốc phiện": "thuốc phiện",
    "cô ca": "côca", "coca": "côca",
}

AMOUNT_RANGE = re.compile(r"từ\s+([\d.,]+)\s*(gam|kilôgam|mililít)\s+đến\s+dưới\s+([\d.,]+)\s*(gam|kilôgam|mililít)", re.I)
AMOUNT_FROM = re.compile(r"(?:khối lượng|thể tích)\s+([\d.,]+)\s*(gam|kilôgam|mililít)\s+trở lên", re.I)
AMOUNT_ANY = re.compile(r"([\d.,]+)\s*(kilôgam|kg|gam|g)\b", re.I)
UNIT_TO_GRAMS = {"gam": 1.0, "g": 1.0, "kilôgam": 1000.0, "kg": 1000.0, "mililít": 1.0, "ml": 1.0}


def plain_key(name: str) -> str:
    """Case/accent-preserving-ish normalization for non-crime entities (people, locations)."""
    return re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())


def to_number(value: str) -> float:
    return float(value.replace(".", "").replace(",", "."))


def amount_to_grams(text: str) -> float | None:
    """'hơn 9,6kg' -> 9600.0; 'gần 406g' -> 406.0; '' -> None."""
    match = AMOUNT_ANY.search(text or "")
    if not match:
        return None
    return to_number(match.group(1)) * UNIT_TO_GRAMS[match.group(2).lower()]


def parse_amount_ranges(text: str) -> list[tuple[float, float | None]]:
    """Ranges in grams found in a law clause: 'từ 30 gam đến dưới 100 gam' -> (30, 100); '... 100 gam trở lên' -> (100, None)."""
    ranges: list[tuple[float, float | None]] = []
    for low, low_unit, high, high_unit in AMOUNT_RANGE.findall(text):
        ranges.append((to_number(low) * UNIT_TO_GRAMS[low_unit.lower()],
                       to_number(high) * UNIT_TO_GRAMS[high_unit.lower()]))
    for low, unit in AMOUNT_FROM.findall(text):
        ranges.append((to_number(low) * UNIT_TO_GRAMS[unit.lower()], None))
    return ranges


def aggregate_range(ranges: list[tuple[float, float | None]]) -> tuple[float | None, float | None]:
    """Collapse the ranges a clause gives for one substance into the tightest (min, max) window."""
    if not ranges:
        return None, None
    lows = [low for low, _ in ranges]
    if any(high is None for _, high in ranges):
        return min(lows), None
    return min(lows), max(high for _, high in ranges)


def canonical_substance(name: str) -> str | None:
    """'ma túy kẹo' / 'thuốc lắc' / 'MDMA' -> 'MDMA'; falls back to a fuzzy match on the law list."""
    key = re.sub(r"\s+", " ", name.strip().lower())
    if not key:
        return None
    if key in SUBSTANCE_SYNONYMS:
        return SUBSTANCE_SYNONYMS[key]
    return link_entity(name, SUBSTANCES, normalize=lambda value: re.sub(r"\s+", " ", value.strip().lower())) or name.strip()


def clause_substance_ranges(text: str) -> dict[str, tuple[float | None, float | None]]:
    """Per-substance amount window for one clause, read line by line so each điểm keeps its own thresholds."""
    gathered: dict[str, list[tuple[float, float | None]]] = {}
    for line in text.splitlines():
        substances = find_substances(line)
        if not substances:
            continue
        ranges = parse_amount_ranges(line)
        for substance in substances:
            gathered.setdefault(substance, []).extend(ranges)
    return {substance: aggregate_range(ranges) for substance, ranges in gathered.items()}


def extract_cases_custom(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one article, then canonicalize charges and substances in code (same prompt as HINT)."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        substances = []
        for item in case.get("substances", []):
            name = canonical_substance(item.get("name", ""))
            if name:
                substances.append({"name": name, "amount": item.get("amount", ""),
                                   "amount_grams": amount_to_grams(item.get("amount", ""))})
        case["substances"] = substances
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
            person["key"] = plain_key(person.get("name", ""))
        case["people"] = [p for p in case.get("people", []) if p.get("name")]
    return cases

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- CUSTOM ontology: writes

    def custom_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "uid"),
                           ("Person", "key"), ("Substance", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article_custom(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            WITH cl, clause
            UNWIND clause.mentions AS mention
            MERGE (sub:Substance {name: mention.name})
            MERGE (cl)-[m:MENTIONS]->(sub)
              SET m.amount_min_g = mention.amount_min_g, m.amount_max_g = mention.amount_max_g
            """,
            id=article["id"], title=article["title"], law=article["law"], doc_id=article["doc_id"],
            crime=article["crime"],
            clauses=[{**clause, "mentions": [
                {"name": name, "amount_min_g": bounds[0], "amount_max_g": bounds[1]}
                for name, bounds in clause_substance_ranges(clause["text"]).items()
            ]} for clause in article["clauses"]],
        )

    def add_news_case_custom(self, case: dict, doc: Document, index: int) -> None:
        people = [{"key": person.get("key") or plain_key(person.get("name", "")),
                   "name": person.get("name", ""), "aliases": person.get("aliases", []),
                   "role": person.get("role", ""), "charge": person.get("charge", ""),
                   "sentence": person.get("sentence", "")}
                  for person in case.get("people", []) if person.get("name")]
        substances = [{"name": canonical_substance(item.get("name", "")), "amount": item.get("amount", ""),
                       "amount_grams": item.get("amount_grams", amount_to_grams(item.get("amount", "")))}
                      for item in case.get("substances", []) if item.get("name")]
        self.run(
            """
            MERGE (k:Case {uid: $uid})
              SET k.name = $name, k.summary = $summary, k.date = $date,
                  k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: $location_key}) SET l.label = loc
                MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name})
                MERGE (k)-[r:INVOLVES]->(sub) SET r.amount = s.amount, r.amount_grams = s.amount_grams)
            FOREACH (p IN $people | MERGE (person:Person {key: p.key})
                SET person.name = p.name, person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            uid=f"{doc.id}#{index}", name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            location_key=plain_key(case.get("location", "")),
            charges=case.get("charges", []), people=people, substances=substances,
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        seed_ids, facts = self.seed_facts(question, doc_ids)
        seen = set(facts)

        def add(fact: str) -> None:
            if fact and fact not in seen:
                seen.add(fact)
                facts.append(fact)

        # a. Cases that are a seed or adjacent to one -> short case summary.
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, coalesce(k.name, k.summary, k.uid) AS name, coalesce(k.summary, '') AS summary
            """,
            ids=seed_ids,
        )
        case_ids = [case["id"] for case in cases]
        for case in cases:
            add(f"Vụ việc '{case['name']}': {case['summary']}")

        # b. Bridge to the other KB: every case's charge -> defining Article -> plausible clauses.
        if case_ids:
            clauses = self.run(
                """
                MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE elementId(k) IN $case_ids
                  AND (cl.number = 1 OR EXISTS { MATCH (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
                RETURN a.id AS article_id, a.title AS title, cl.number AS number, cl.text AS text
                ORDER BY article_id, number
                """,
                case_ids=case_ids,
            )
            for clause in clauses:
                add(_clause_fact(clause))

            # Custom threshold hop: when the case states an amount, pick the exact khoản whose window contains it.
            exact = self.run(
                """
                MATCH (k:Case)-[inv:INVOLVES]->(:Substance)<-[m:MENTIONS]-(cl:Clause)<-[:HAS_CLAUSE]-(a:Article)
                WHERE elementId(k) IN $case_ids
                  AND inv.amount_grams IS NOT NULL AND m.amount_min_g IS NOT NULL
                  AND (k)-[:CHARGED_WITH]->()<-[:DEFINES]-(a)
                  AND inv.amount_grams >= m.amount_min_g
                  AND (m.amount_max_g IS NULL OR inv.amount_grams < m.amount_max_g)
                RETURN DISTINCT a.id AS article_id, a.title AS title, cl.number AS number, cl.text AS text
                ORDER BY article_id, number
                """,
                case_ids=case_ids,
            )
            for clause in exact:
                add(_clause_fact(clause))

        # c. Articles named directly in the question ("Điều 251" -> "251").
        article_numbers = re.findall(r"[Đđ]iều (\d+)", question)
        if article_numbers:
            clauses = self.run(
                """
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
                WHERE any(n IN $numbers WHERE a.id CONTAINS ('Điều ' + n))
                  AND (cl.number = 1 OR any(s IN $substances WHERE EXISTS { (cl)-[:MENTIONS]->(:Substance {name: s}) }))
                RETURN a.id AS article_id, a.title AS title, cl.number AS number, cl.text AS text
                ORDER BY article_id, number
                """,
                numbers=article_numbers, substances=find_substances(question),
            )
            for clause in clauses:
                add(_clause_fact(clause))

        return facts[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def _clause_fact(clause: dict) -> str:
    return f"[{clause['article_id']} - {clause['title']}] khoản {clause['number']}: {clause['text']}"


KG_ONTOLOGY = os.getenv("KG_ONTOLOGY", "custom").strip().lower()


def _build_hint(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document], llm_fn: Callable[..., str]) -> None:
    """Suggested ontology: call the HINT helpers as-is (used to produce ket_qua_benchmark_kg.hint.txt)."""
    graph.suggested_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]
    for article in articles:
        graph.add_law_article(article)
    crimes = [article["crime"] for article in articles if article["crime"]]
    for doc in news_docs:
        for case in extract_news_cases(doc, lambda prompt: llm_fn(prompt, json_mode=True), crimes):
            graph.add_news_case(case, doc)


def _build_custom(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document], llm_fn: Callable[..., str]) -> None:
    """Custom ontology: stable identity keys, canonical substances, amount thresholds."""
    graph.custom_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]
    for article in articles:
        graph.add_law_article_custom(article)
    crimes = [article["crime"] for article in articles if article["crime"]]
    for doc in news_docs:
        for index, case in enumerate(extract_cases_custom(doc, lambda prompt: llm_fn(prompt, json_mode=True), crimes)):
            graph.add_news_case_custom(case, doc, index)


def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat).

    Contract: every node created from ONE document carries `doc_id` = Document.id. Shared bridge nodes
    (Crime, Substance, Person, Location) are intentionally document-independent.
    Set KG_ONTOLOGY=hint to build the suggested ontology instead (bonus baseline).
    """
    if KG_ONTOLOGY == "hint":
        _build_hint(graph, law_docs, news_docs, llm_fn)
    else:
        _build_custom(graph, law_docs, news_docs, llm_fn)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(chunk["metadata"]["doc_id"] for chunk in chunks))
        facts = self.graph.context(question, doc_ids)
        facts_text = "\n".join(f"- {fact}" for fact in facts) or "- (không có dữ kiện graph)"
        chunks_text = "\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, start=1))
        prompt = GRAPH_PROMPT.format(facts=facts_text, chunks=chunks_text, question=question)
        return self.llm_fn(prompt)
