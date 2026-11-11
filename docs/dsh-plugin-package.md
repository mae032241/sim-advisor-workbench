# DSH 插件打包

完整插件为 `@local/sim-advisor-workbench`，当前版本 **0.2.48**。插件包含工作台 Python 源码、CLI、网页、规划提示词、DSH Bridge 和资源清单。COMSOL、官方资料、用户模型、资料库索引、模型权重、Python/Node 运行环境及依赖二进制均由用户自行准备。

## 构建与安装

需要 Python 3.12+；插件运行需要 DSH 和 Node.js 24.9+。

```powershell
python scripts/build_dsh_workbench.py
```

默认生成 `dist/sim-advisor-workbench-0.2.48.tgz`、资源清单和 SHA256 文件。也可用 `--out-dir <输出目录>` 指定目标。构建器按源码白名单组装 `runtime/`、`bridge/` 和其它插件资源，并复核压缩包内容。

在 DSH 官方插件管理器中安装生成的 `.tgz`。插件源码目录不含完整打包资源，不能直接安装为完整插件。若已使用旧的独立 Bridge，先停用重复工具提供方，再启用完整插件。

## 配置与启动

在插件设置中配置工作台数据目录和 Python；按需要配置解析器、QMD、独立 Node.js、PageIndex、COMSOL 与资料目录。也可选择自行准备且符合初始化器格式的离线依赖目录。具体字段见 [config.js](../integrations/dsh-sim-workbench/config.js) 和 [可选依赖说明](offline-dependencies.md)。

侧栏或 `start_workbench` 按需启动或复用本插件工作台；`workbench_status` 查看状态；`create_workbench_plan` 创建并绑定当前 DSH 会话。启动服务不等于创建方案或开始模型调用。停止工作台只针对本插件拥有的服务。

默认检索使用 native，QMD 关键词为可选扩展，向量构建和语义增强需显式选择。embedding 默认关闭。运行和数据迁移说明见 [DSH 接入](dsh-workbench.md)。

## 发布检查

源码发布与依赖分发是不同交付物。构建插件不会自动提供依赖二进制或官方资料；发布者应核对生成包的实际资源清单。固定官方测试集、benchmark 资料和本地评测结果不随源码发布。

插件保留了 [vendor/](../integrations/dsh-sim-workbench/vendor/) 中的第三方源码闭包、清单及原始许可证；这些组件遵循各自许可。不要据此为整个项目推定统一许可证。
