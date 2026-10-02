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

## 安装

```bash
pip install -r requirements.txt
```

## 使用

```bash
export NIM_API_KEY="sk-xxxx"            # 必填：你的 NIM 端点 key
# 把待解析的 PDF 放到 ./pdfs/ 下（lec01.pdf, assn01.pdf ...）
python parse_concurrent.py             # 解析全部 PDF -> ./pdfs/md/<name>_p<页>.md
```

可调环境变量：`NIM_ENDPOINT` / `NIM_MODEL` / `PDF_DIR` / `OUT_DIR` / `WORKERS`(并发数) / `DPI`。

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
| `requirements.txt` | 依赖（仅 pymupdf） |
| `.env.example` | 环境变量模板（key 切勿提交） |
