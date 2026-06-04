# PDF 英译中（pdf2zh-next）

基于 [PDFMathTranslate-next](https://github.com/PDFMathTranslate/PDFMathTranslate-next)（`pdf2zh-next`），在保留公式、图表、目录等排版的前提下，将 PDF 从英文翻译成简体中文。

## 快速开始

### 1. 启动 Web UI

```bash
chmod +x start-webui.sh
./start-webui.sh
```

浏览器打开：**http://localhost:7860/**

将 PDF 拖入页面，选择翻译服务（如 Bing、Google 等），点击 **Translate**。

### 2. 命令行翻译（可选）

```bash
source .venv/bin/activate
pdf2zh_next your.pdf --bing --lang-in English --lang-out "Simplified Chinese" --output ./output
```

输出目录中会生成双语/单语 PDF。

## 翻译服务说明

Web UI 中需选择可用的翻译后端，例如：

| 服务 | 说明 |
|------|------|
| **Bing** | 一般无需 API Key，适合试用 |
| **Google** | 需按官方文档配置 |
| **DeepL / OpenAI / Ollama** | 需在界面或配置文件中填写 API / 本地模型 |

具体配置见官方文档：<https://pdf2zh-next.com/>

## 环境变量

| 变量 | 默认值 | 含义 |
|------|--------|------|
| `PDF2ZH_LANG_FROM` | English | 源语言 |
| `PDF2ZH_LANG_TO` | Simplified Chinese | 目标语言 |
| `PDF2ZH_SERVER_PORT` | 7860 | Web 端口 |
| `PDF2ZH_UI_LANG` | zh | 界面语言 |

## 目录结构

```
.
├── .venv/              # Python 3.12 虚拟环境（已安装 pdf2zh-next）
├── start-webui.sh      # 启动 Web UI
└── README.md
```

## 重新安装依赖

若 `.venv` 损坏，可执行：

```bash
uv venv --python 3.12 .venv
uv pip install pdf2zh-next
```

## 参考

- 官方 Web UI 说明：<https://pdf2zh-next.com/getting-started/USAGE_webui.html>
- 项目仓库：<https://github.com/PDFMathTranslate/PDFMathTranslate-next>
