# 检索与来源读取

工作台将检索候选、原文读取和方案引用分别处理。搜索摘要只能帮助选择来源；用于方案的证据须通过 `read_source` 读取，关联相应方案后再引用。

## 检索模式

| 模式 | 行为 |
| --- | --- |
| `native` | 默认，SQLite FTS5/BM25 关键词检索 |
| `qmd_keyword` | 可选 QMD 关键词检索，需自备 QMD 运行环境和索引 |
| `qmd_vector` | 手动配置 embedding 并显式构建后的向量检索 |

embedding 默认关闭。普通启动和关键词检索不自动下载权重或构建向量。检索范围遵循资料库启停、类型、功能分支及测试集隔离配置。依赖可用、某个索引已构建和实际查询可用分别报告，不能互相替代。

## 登记自备资料

```powershell
python library_cli.py add "我的手册" --type user-manuals --path "<资料目录>"
python library_cli.py build LIBRARY_ID --modes native
python library_cli.py enable LIBRARY_ID
python library_cli.py search "网格收敛" --mode native --library LIBRARY_ID
python library_cli.py read SOURCE_ID
```

支持 `official-manuals`、`official-models`、`user-manuals`、`user-models`；分类是注册声明，不从路径推断出版方身份。新增库默认停用。构建的默认模式为 native；`library_cli.py index` 默认 native 与 QMD 关键词，运行前应确认可选依赖已配置。

模型配置及代码证据可通过 `configuration` 和 `evidence` 子命令按返回的引用读取。原始来源由用户提供，索引与派生资料写入工作台数据目录。完整 CLI 参数见 `python library_cli.py --help`。

## Agent 与 API

公共工具 `get_library_scope` 查询包括关闭库在内的轻量目录，`set_libraries_enabled` 批量启停指定库。`search_knowledge` 可使用 `library_ids` 和 `feature_branch` 限定范围；`read_source` 分页读取正文。参数以 `GET /api/tools` schema 为准。

为方案研究时显式传工作台 `session_id`，读取记录才关联该方案。独立检索可省略此 ID。关闭或删除的库不参与新检索，旧引用的新读取也执行当前可见性检查；库开关不会重新构建索引。

## 可选 PDF 路径

普通 PDF 文本提取使用可选 pypdf。PageIndex 是另一条显式 PDF 推理适配，需要自备 SDK、模型和凭据；不会自动索引资料库，也不把 HTML 目录检索标为 PageIndex。

HTML 中的公式图片和图示未 OCR，原文上下文和数学记号需回到原始资料核对。检索相关性、来源匹配和结构化方案保存均不能证明仿真正确。

依赖配置见 [可选依赖](../docs/offline-dependencies.md)。实现见 [library_search.py](../advisor/library_search.py)、[knowledge.py](../advisor/knowledge.py)、[retrieval_backends.py](../advisor/retrieval_backends.py) 和 [planning_service.py](../advisor/planning_service.py)。本仓库不含官方资料、预建索引或固定检索评测集。
