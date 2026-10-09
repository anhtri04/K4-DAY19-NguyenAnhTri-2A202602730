# Báo cáo Day 19 — Flat RAG vs GraphRAG

**Họ tên:** Nguyễn Anh Trí  **MSSV:** 2A202602730  **Ngày:** 2026-10-09

> Kỳ vọng và thang điểm: `SUBMISSION.md`. Mọi số liệu phải khớp với `ket_qua_benchmark_kg.txt`. Bản thiết kế ontology nộp riêng ở `report/ONTOLOGY.md`.
> Cấu hình chạy: chat = `openai:glm-5.3-flash` (OpenCode Go, OpenAI-compatible), embedding = `local:text-embedding-bge-m3` (LM Studio), `top_k=3`, `chunk_size=800`, 176 chunk, KG 206 node / 393 cạnh.

## 1. Chi phí (10 điểm)

Hai bảng copy từ `ket_qua_benchmark_kg.txt`:

```
== Indexing (one-off)
pipeline  calls    in_tok  out_tok       USD  seconds
flat        176         0        0   0.00000     64.1
graph       196     35921     5090   0.00793    214.3

== Querying (mean per question)
pipeline  recall  judge   in_tok  out_tok       USD  seconds
flat        0.57   1.50      721       69   0.00014     3.21
graph       0.94   1.83     5081      153   0.00084    17.34
```

| Chỉ số | Flat | Graph | Graph / Flat |
| --- | --- | --- | --- |
| Indexing USD | 0.00000 | 0.00793 | — (Flat = 0 vì embedding chạy local miễn phí; Graph thêm ~$0.008 cho trích xuất) |
| Indexing giây | 64.1 | 214.3 | ×3.34 |
| Mỗi câu: USD | 0.00014 | 0.00084 | ×6.0 |
| Mỗi câu: giây | 3.21 | 17.34 | ×5.40 |
| Mỗi câu: in_tok | 721 | 5081 | ×7.05 |

**Chi phí tăng thêm đến từ đâu?**
1. **Lúc dựng:** GraphRAG gọi LLM 20 lần để trích xuất tin tức (35.921 in-tok + 5.090 out-tok ≈ $0.008); embedding không tốn vì chạy local. Flat RAG chỉ embed 176 chunk (local, $0). Đây là chi phí một lần.
2. **Mỗi câu hỏi:** prompt GraphRAG dài gấp ~7 lần (5.081 vs 721 in-tok) vì phải nhồi thêm dữ kiện graph — các khoản luật có `text` rất dài. Vì prompt dài hơn nên USD ×6 và độ trễ ×5.4 (còn do endpoint Go + số token sinh ra nhiều hơn).
3. **Hòa vốn:** Graph đắt hơn cả khi dựng lẫn mỗi câu, nên **không bao giờ rẻ hơn** Flat về chi phí. Toàn bộ một lần chạy chỉ chênh ~$0.012 ($0.00793 một lần + 6 × $0.00070). Vì vậy quyết định dùng KG phải dựa vào **độ chính xác**, không phải tiền: chỉ cần một câu cross-KB trả lời đúng thay vì sai là đã đáng.

## 2. Từng câu hỏi (10 điểm)

| Câu | Loại | Flat recall / judge | Graph recall / judge | Thắng | Vì sao (1 câu) |
| --- | --- | --- | --- | --- | --- |
| Q1 | single-hop-law | 1.00 / 2 | 1.00 / 2 | hòa | Đáp án nằm gọn trong 1 khoản luật, vector search đã đủ. |
| Q2 | single-hop-news | 1.00 / 2 | 1.00 / 2 | hòa | Đáp án nằm gọn trong 1 bài báo, graph không thêm gì. |
| Q3 | cross-kb | 0.33 / 1 | **1.00 / 2** | graph | Cần mức án (tin) + Điều 251 (luật); Flat thiếu nửa sau. |
| Q4 | cross-kb | 0.33 / 1 | **0.67 / 1** | graph | Cần Điều 255; graph nối được nhưng chỉ lấy khoản 1 nên hụt khung tối đa (xem E2). |
| Q5 | cross-kb-multi-hop | 0.40 / 1 | **1.00 / 2** | graph | Ngưỡng khối lượng MDMA chọn đúng khoản 4 Điều 250 (tử hình/chung thân). |
| Q6 | aggregation | 0.33 / 2 | **1.00 / 2** | graph | Gộp nhiều vụ theo `Substance` đã chuẩn hóa (MDMA). |

**Quy luật rút ra:** khi đáp án nằm trong **một** chunk (single-hop) hai pipeline hòa nhau; khi câu hỏi cần **nối ≥2 nguồn** (cross-kb, multi-hop) hoặc **gộp qua nhiều vụ** (aggregation), GraphRAG thắng rõ — recall trung bình 0.57 → 0.94, judge 1.50 → 1.83.

## 3. Phân tích lỗi (20 điểm)

### Lỗi E2: Thiếu ngữ cảnh luật — câu trả lời sai khung hình phạt tối đa (Q4)

- **Hiện tượng:** Q4 GraphRAG đạt recall 0.67 / judge 1, trả lời "mức phạt tù tối đa… là **7 năm tù**", trong khi đáp án chuẩn là "tù 20 năm hoặc tù chung thân" (khoản 4 Điều 255).
- **Bằng chứng:** trích câu trả lời Graph Q4 từ `ket_qua_benchmark_kg.txt`:

```
Theo Điều 255 BLHS - Tội tổ chức sử dụng trái phép chất ma túy, khoản 1: "...thì bị phạt tù từ 02 năm đến 07 năm."
Ngữ cảnh chỉ cung cấp khoản 1 của Điều 255, nên theo dữ kiện có sẵn, mức phạt tù tối đa được nêu là 7 năm tù.
```

Cypher cho thấy mọi khoản của Điều 255 đều không `MENTIONS` chất nào:

```cypher
MATCH (a:Article {id:'Điều 255 BLHS'})-[:HAS_CLAUSE]->(cl:Clause)
OPTIONAL MATCH (cl)-[:MENTIONS]->(s:Substance)
RETURN cl.number AS clause, cl.penalty AS penalty, collect(s.name) AS mentions ORDER BY clause
```

```
{'clause': 1, 'penalty': 'phạt tù từ 02 năm đến 07 năm', 'mentions': []}
{'clause': 2, 'penalty': 'phạt tù từ 07 năm đến 15 năm', 'mentions': []}
{'clause': 3, 'penalty': 'phạt tù từ 15 năm đến 20 năm', 'mentions': []}
{'clause': 4, 'penalty': 'phạt tù 20 năm hoặc tù chung thân', 'mentions': []}
{'clause': 5, 'penalty': 'phạt tiền ...', 'mentions': []}
```

- **Nguyên nhân:** ở KG-3 (`context`), quy tắc lọc khoản giữ **khoản 1 + các khoản `MENTIONS` một chất mà vụ `INVOLVES`**. Quy tắc này giả định hình phạt gắn với khối lượng chất. Nhưng Điều 255 (tổ chức sử dụng) **không nhắc chất**, nên không khoản nào khớp và graph chỉ còn khoản 1 → mất khung tối đa. Lỗi nằm ở **thiết kế ontology/Cypher KG-3**, không phải ở LLM.
- **Đề xuất sửa:** khi câu hỏi hỏi "tối đa" (hoặc khi Điều không `MENTIONS` chất), lấy thêm khoản có `number` cao nhất / khoản có `penalty` chứa "chung thân" hoặc "tử hình". Đánh đổi: prompt dài hơn (thêm 1–2 khoản), tốn token hơn một chút nhưng sửa đúng lỗi sai nghiêm trọng.

### Lỗi E3: Trùng thực thể — một chất thành nhiều node

- **Hiện tượng:** `Substance` có cả `etomidate` và `Etomidate` là hai node khác nhau; ngoài ra có tên không chuẩn `ma túy tổng hợp`.
- **Bằng chứng:**

```cypher
MATCH (s:Substance) RETURN s.name AS name ORDER BY toLower(name)
```

```
Amphetamine, Cocaine, côca, cần sa, etomidate, Etomidate, Heroine, Ketamine,
ma túy tổng hợp, MDMA, Methamphetamine, thuốc phiện, XLR-11
```

- **Nguyên nhân:** `canonical_substance` chỉ có bảng đồng nghĩa cho các chất trong danh sách `SUBSTANCES`; `etomidate` không nằm trong danh sách nên rơi vào nhánh dự phòng `or name.strip()` — giữ nguyên chữ hoa/thường. Vì khóa định danh là `MERGE (s:Substance {name})`, hai cách viết hoa khác nhau tạo hai node. Đây là lỗi ở **bước chuẩn hóa tên (KG-1/KG-2) + thiết kế khóa**, không phải lỗi graph.
- **Đề xuất sửa:** mở rộng `SUBSTANCES`/bảng đồng nghĩa (thêm `etomidate`), và chuẩn hóa tên chất lạ bằng khóa lowercase (`plain_key`) trước khi `MERGE`; loại `ma túy tổng hợp` khỏi `Substance` (là nhóm, không phải chất). Đánh đổi: lowercase mù có thể gộp nhầm hai chất thật sự khác nhau — cần cân nhắc.

### Lỗi E6: Thuộc tính thiếu trên quan hệ — chỗ hợp lý vs chỗ là lỗi trích xuất

- **Hiện tượng:** nhiều cạnh `INVOLVED_IN` có `charge=''` hoặc `sentence=''`.
- **Bằng chứng:**

```cypher
MATCH (p:Person)-[r:INVOLVED_IN]->(k)
WHERE r.charge='' OR r.sentence=''
RETURN p.name AS person, r.role AS role, r.charge AS charge, r.sentence AS sent, k.name AS case
ORDER BY person LIMIT 6
```

```
{'person':'7 công dân Trung Quốc (không nêu tên)','role':'nghi phạm','charge':'vận chuyển trái phép chất ma túy','sent':''}
{'person':'Anh Hai','role':'người liên quan','charge':'','sent':''}
{'person':'Cái Quang Huy','role':'bị can','charge':'vận chuyển trái phép chất ma túy','sent':''}
{'person':'Cao Thị Bích Hằng','role':'bị cáo','charge':'','sent':''}
{'person':'Dương Văn Biết','role':'bị cáo','charge':'','sent':''}
{'person':'Dương Minh Tuấn','role':'nghi phạm','charge':'tổ chức sử dụng trái phép chất ma túy','sent':''}
```

- **Nguyên nhân:** `sentence=''` cho `nghi phạm`/`bị can` (Cái Quang Huy, Dương Minh Tuấn) là **hợp lý** — báo chưa có mức án vì chưa xét xử. Nhưng `charge=''` ở người đã là `bị cáo` trong một vụ có tội danh rõ (Cao Thị Bích Hằng, Dương Văn Biết) là **lỗi trích xuất**: prompt để `charge` là trường tùy chọn nên LLM bỏ trống dù vụ đã có `CHARGED_WITH`.
- **Đề xuất sửa:** sau trích xuất, nếu `charge` rỗng nhưng `Case` chỉ có **đúng một** `CHARGED_WITH`, suy ra `charge` của người = tội của vụ. Đánh đổi: vụ nhiều bị cáo / nhiều tội dễ gán sai, nên chỉ suy khi vụ có một tội.

### Lỗi E4: Phép đo sai — recall thấp giả tạo (Q6, Flat)

- **Hiện tượng:** Q6 Flat có `recall=0.33` nhưng `judge=2` (nội dung đúng).
- **Bằng chứng:** `must_include` của Q6 là `["Cái Quang Huy", "Lê Minh Thành", "Pháp y tâm thần"]`, nhưng câu trả lời Flat chỉ gọi tắt:

```
1. Đông (bệnh nhân Viện Pháp y tâm thần Trung ương)... 2. Thành: bị bắt quả tang khi mang 5 viên "kẹo"...
```

Câu trả lời nêu đúng sự việc nhưng không chứa đúng chuỗi "Lê Minh Thành" / "Cái Quang Huy" → `keyword_recall` khớp chuỗi con tính là trượt.
- **Nguyên nhân:** lỗi ở **phép đo** `keyword_recall` (khớp chuỗi chính xác, không hiểu alias/viết tắt), không phải lỗi của pipeline.
- **Đề xuất sửa:** chấm recall qua `link_entity`/alias, hoặc dùng thêm alias trong `must_include`, hoặc chỉ tin `judge` cho câu aggregation. Đánh đổi: recall rẻ và khách quan nhưng thô; judge linh hoạt nhưng chính nó cũng có thể sai.

## 4. Kết luận (5 điểm)

- **Khi nào nên dùng KG:** khi câu hỏi **nối nhiều nguồn** (cross-kb, multi-hop) hoặc **gộp qua nhiều vụ** (aggregation). Bằng chứng: Q3 recall 0.33 → 1.00, Q5 0.40 → 1.00, Q6 0.33 → 1.00; recall trung bình 0.57 → 0.94, judge 1.50 → 1.83.
- **Khi nào Flat RAG đủ:** khi đáp án nằm gọn trong một đoạn (Q1, Q2 hòa 1.00/2) — dùng KG chỉ tốn thêm token mà không tăng chất lượng.
- **Chi phí:** Graph đắt hơn ×6 USD/câu, ×5.4 thời gian/câu, ×3.3 thời gian dựng, nhưng tuyệt đối rất nhỏ (~$0.012/lần chạy vì embedding local). Ở quy mô lớn, chi phí dựng trả một lần, còn per-query tăng do prompt dài (text khoản luật) — nên **KG đáng tiền khi tỉ lệ câu hỏi cross-KB cao và độ chính xác quan trọng hơn token**, đặc biệt khi cầu nối `Crime` và ngưỡng khối lượng được mô hình hóa (như thiết kế tự chọn ở đây).
- **Điều kiện cụ thể:** dữ liệu nhiều nguồn có **entity cầu nối rõ ràng**; nếu mọi câu đều single-hop thì Flat RAG rẻ và đủ.

## 5. Tự kiểm (5 điểm)

```
$ pytest tests/ -q
................................................                         [100%]
48 passed in 0.03s

$ python bench_kg.py --check
[OK] Dữ liệu: 18 điều luật, 20 bài báo
[OK] KG-1 link_entity
[OK] Neo4j kết nối được
[provider] chat = openai:glm-5.3-flash | embedding = local:text-embedding-bge-m3
[OK] KG-2 build_graph: 148 node / 294 cạnh, đường xuyên 2 KB dài 2 cạnh
[OK] KG-3 context: 23 dữ kiện, có Điều 251
[OK] KG-4 GraphRAGAgent.answer
[OK] Chi phí check: 1 lần gọi LLM, $0.00071.
```

Ảnh Neo4j: `report/img/kg_count.png`, `report/img/kg_cross_kb.png`, `report/img/kg_my_case.png`.
Người đã chọn cho `kg_my_case.png`: **Cái Quang Huy** (Person → Case → CHARGED_WITH → Crime ← DEFINES → Điều 250, kèm INVOLVES MDMA/Ketamine).

## Vấn đề gặp phải (không tính điểm)

- Ban đầu định chạy local hoàn toàn (LM Studio) nhưng máy không có GPU và tải model chat quá chậm, nên chuyển sang **chat hosted (OpenCode Go, OpenAI-compatible) + embedding local bge-m3**. `src/llm.py` được thêm nhà cung cấp `local` và hỗ trợ `<PROVIDER>_EXTRA_HEADERS` để gửi header `x-opencode-session` mà OpenCode Go yêu cầu. Base RAG và `bench_kg.py` không bị sửa.
- Số node/cạnh thay đổi nhẹ giữa các lần chạy (ví dụ 206 vs 207 node) do trích xuất LLM không tất định; báo cáo dùng đúng số trong `ket_qua_benchmark_kg.txt`.
