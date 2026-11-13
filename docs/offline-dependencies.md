# 可选依赖与离线配置

本仓库发布源码，不包含离线依赖包、依赖二进制、Python/Node 运行环境、模型权重、COMSOL 安装或资料库。请按所需功能自行准备依赖和有权使用的输入。

| 功能 | 所需环境 |
| --- | --- |
| 本地 HTTP 工作台和基础工具 | Python 3.12+，标准库 |
| MCP stdio 桥接 | `python -m pip install -r requirements-mcp.txt` |
| DSH 插件 | DSH、Node.js 24.9+、Python 3.12+ |
| STEP/MPH 扩展解析 | 可选 sim-cli/simparse；STEP 内核补充可选 Gmsh |
| QMD 关键词检索 | 已配置的 Node、QMD SDK 与用户构建的索引；无需 embedding 权重 |
| QMD 关键词检索 | 用户提供的 QMD SDK、兼容 Node.js 和对应原生依赖 |
| QMD 向量检索 | 额外配置本地 embedding 权重并明确启用、构建向量 |
| 普通 PDF 文本解析 | 可选 pypdf |
| PageIndex PDF 推理 | 可选 SDK、用户配置的模型与凭据 |
| COMSOL Deep API 读取 | 用户可用的 COMSOL 安装及相应运行环境 |

模型解析细节见 [MODEL_INTEGRATION.md](../MODEL_INTEGRATION.md)，检索细节见 [RETRIEVAL.md](../scripts/RETRIEVAL.md)。安装 SDK 不等于对应输入已解析、索引已构建或模型推理可用。

## 配置方式

独立服务可通过环境变量配置：

- `SIM_ADVISOR_DATA`：工作台持久数据目录。
- `COMSOL_DOC_ROOT`、`COMSOL_APPLICATIONS`：用户提供的文档与案例根目录。
- `SIM_ADVISOR_SIM_PYTHON`：带有解析扩展的 Python。
- `SIM_ADVISOR_NODE`、`SIM_ADVISOR_QMD_DIR`：QMD 检索运行环境。
- `SIM_ADVISOR_PAGEINDEX_PYTHON`、`SIM_ADVISOR_PAGEINDEX_MODEL`：可选 PDF 推理配置。
- `SIM_ADVISOR_COMSOL_HOME`：Deep API 使用的 COMSOL 安装目录。

DSH 插件提供相应设置字段，见 [config.js](../integrations/dsh-sim-workbench/config.js)。插件也可读取工作台数据目录中的 `dependency-profile.json`；该配置用于关联用户自行准备的离线依赖目录。读取器校验版本、目录边界、文件类型和可用的清单摘要，格式见 [dependency-profile.js](../integrations/dsh-sim-workbench/dependency-profile.js)。不要复制其它机器的绝对路径或虚构校验值。

仓库中的 [离线初始化器](../integrations/offline-dependencies/initialize.mjs) 和构建脚本用于组装、校验自备依赖，不代表仓库已提供可直接下载的运行包。离线准备不包含在线规划模型；使用远程模型服务仍需要网络与用户凭据。服务运行期间改变依赖设置后，应停止并重新启动服务。

## embedding

embedding 默认关闭。需在资料库管理中提供本地 GGUF 路径并明确保存启用，再选择向量索引模式。普通启动和 QMD 关键词检索无需开启 embedding。权重与关键词索引分别管理，关键词可用不能证明向量索引已就绪。

自行分发依赖时，应保留实际组件的原始许可证、通知和适用的源码提供材料。仓库保留的第三方源码清单及许可证见 [vendor/](../integrations/dsh-sim-workbench/vendor/)，不为外置依赖或权重推定统一许可。
