# nvidia-nim-parse

用 NVIDIA NIM 的 **`nvidia/nemotron-parse`** 把文档解析为 Markdown 的并发工具。

支持把 **PDF / PPTX / DOCX** 转成结构化 Markdown（表格、标题层级、公式都能识别）。

## 原理

`nvidia/nemotron-parse` 是一个**视觉多模态模型**，输入是一张「文档页面图片」，输出是 Markdown。
它本身**不直接吃 PDF/PPTX/DOCX 字节**，所以流程是：

```
源文件(PDF/PPTX/DOCX)
  └─ 渲染成每页图片(PNG)
       └─ 调 nvidia/nemotron-parse (image_url)
            └─ 返回 Markdown
```

- PDF：用 PyMuPDF 直接逐页渲染 PNG（无需额外依赖）。
- PPTX / DOCX：先由 LibreOffice 转成 PDF，再走上面的 PDF 流程（本仓库默认处理 PDF；
  若需直接喂 PPTX/DOCX，先 `soffice --headless --convert-to pdf` 即可复用同一套代码）。

## 接口要点（踩坑记录）

1. 该模型只走 OpenAI 兼容的 `/v1/chat/completions`，没有 `/v1/infer`。
2. `content` 里**只能有单个 `image_url` 块**；一旦带 `{"type":"text",...}` 就报
   `400 Content cannot be a plain string`。
3. 解析结果**不在 `content`，而在 `tool_calls`**：`function.name = "markdown_bbox"`，
   `arguments` 是 JSON 字符串，解析后为 `[[{bbox, text, type}, ...]]`
   （外层=页，内层=文字块；`type` 如 `Section-header`/`Title`/`Text`/`Page-footer`）。
4. 模型名：`nvidia/nemotron-parse`（v1）可用；`nvidia/nemotron-parse-2.0` 在部分代理端点返回 502，暂不可用。
5. 接口**偶发返回空结果**（HTTP 200 但无内容），已按可重试失败处理；重试耗尽会记为 `err`，
   断点续跑会重新解析该页，不会留下 0 字节文件。

## 扫描件（无文本层 PDF）的预处理

中文扫描件识别错字，**大多不是模型的问题，而是喂图方式**。

本仓库的策略：检测到页面无文本层时，**直接提取 PDF 内嵌的扫描原图**，而不是用
`get_pixmap(dpi=...)` 渲染——渲染只是对低分辨率 JPEG 做插值放大，不产生新信息，
反而放大伪影。随后做 `UPSCALE` 倍 LANCZOS 放大 + 二值化，压掉 JPEG 压缩噪点。

实测（982×1425、约 141 DPI 的扫描页）：

| 喂图方式 | 识别效果 |
|---|---|
| 200/300/400 DPI 渲染 | 差，且随 DPI 无改善 |
| 原生图直接送 | 较好 |
| **原生图 + 2×放大 + 二值化** | **最好，整段基本全对** |

新增环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `UPSCALE` | `2` | 放大倍数 |
| `BINARIZE` | `1` | 二值化去噪点；插图页可设 `0` |
| `THRESHOLD` | `160` | 二值化阈值 |

**硬限制**：若扫描件有效 DPI 太低（<150），插图里的文字像素已不足，任何 OCR 都无法
完全还原。此类页面建议换更高分辨率的扫描源，或用本地 OCR（PaddleOCR / MinerU）
专门处理插图文字。

## 安装

```bash
pip install -r requirements.txt
cp .env.example .env                    # 填入 NIM_API_KEY（脚本会自动加载 .env）
```

## 使用

```bash
# 把待解析的 PDF 放到 ./pdfs/ 下（lec01.pdf, assn01.pdf ...）
python parse_concurrent.py             # 解析全部 PDF -> ./pdfs/md/<name>_p<页>.md
```

可调环境变量（写在 `.env`，或直接 export 覆盖）：
`NIM_ENDPOINT` / `NIM_MODEL` / `PDF_DIR` / `OUT_DIR` / `WORKERS`(并发数) / `DPI` /
`UPSCALE` / `BINARIZE` / `THRESHOLD`。

单人验证（`parse_demo.py`）默认解析 `lec01/lec04/assn01` 第 1 页。

## 速度

- 渲染单页 ~0.06s（可忽略），瓶颈 100% 在接口调用。
- 单页约 10–13s（网络有波动，偶尔单页会卡到 1–3 分钟）。
- **并发收益极大**：10 路并发时约 50 页/分钟；串行 347 页约 73 分钟，并发约 7 分钟（快约 10×）。

## 文件

| 文件 | 说明 |
|------|------|
| `parse_concurrent.py` | 主工具：并发解析 + 失败重试 + 断点续跑 |
| `parse_demo.py` | 单页/少量页示例，用于验证接口 |
| `requirements.txt` | 依赖（pymupdf / pillow / python-dotenv） |
| `.env.example` | 环境变量模板（key 切勿提交） |
