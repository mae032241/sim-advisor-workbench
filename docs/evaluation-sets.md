# 测试集隔离

测试集是已有资料库案例的逻辑分组，用于在规划评测时限制检索和来源读取。配置不会复制 MPH 或教程，不改变已有来源 ID，也不重新生成 native/QMD 索引。

本仓库不包含固定官方测试集、benchmark 资料、参考答案或既有评测结果。请使用自己有权处理的资料，独立准备选择清单、公开输入、私有参考与验收标准。

## 配置自备选择清单

可基于已有结构化资料生成候选选择：

```powershell
python scripts/select_evaluation_cases.py --data-dir "<工作台数据目录>" --output selection.json
```

请复核和编辑生成清单；配置差异不证明语义独立或工程代表性。随后显式提供 `--selection`：

```powershell
python scripts/configure_evaluation_set.py --data-dir "<工作台数据目录>" --selection selection.json --dry-run
python scripts/configure_evaluation_set.py --data-dir "<工作台数据目录>" --selection selection.json
```

同名集合替换，其余集合保留。参数中的数据目录是工作台数据根，脚本在其中访问 `libraries/`。配置格式和校验以 [evaluation_sets.py](../advisor/evaluation_sets.py) 为准。

普通建库及 Deep API 发布会刷新既有隔离映射；其它离线变更后可显式刷新：

```powershell
python scripts/configure_evaluation_set.py --data-dir "<工作台数据目录>" --refresh
```

## 开放与隔离

网页资料库管理的“测试集”页提供全局检索开关，缺省保持隔离。开放测试集只解除额外隔离，仍遵循资料库启用状态。Agent 工具和普通资料库开关不修改该开关。

隔离涵盖已关联的模型节点、历史来源、同源副本和教程引用。native/QMD 在候选截取前排除测试内容，正文、配置与证据读取也检查可见性。目录变化而映射尚未刷新时，检索会拒绝使用过期隔离配置。

这是业务接口隔离，不能限制另有本地文件或终端权限的 Agent。盲测应另外控制其可访问输入。几何挑战需显式提供自备 `--scenario`，benchmark 工具需用 `--root` 指向自备且符合其格式的资料；源码中的工具不自带这些测试材料，见 [几何挑战说明](../scripts/geometry_challenge/README.md)。
