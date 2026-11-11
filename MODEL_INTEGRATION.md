# 模型与几何读取

`advisor.model_reader.inspect_model(path)` 接收 STEP/STP 或保存的 COMSOL MPH，返回工作台快照。读取器打开原始文件进行检查，不修改输入，不执行 COMSOL 几何、网格或求解。

## 可选解析器

主服务使用 Python 3.12+。扩展解析可使用用户自行安装的 sim-cli/simparse；STEP 的内核拓扑、边界框和体积补充可使用 Gmsh。`SIM_ADVISOR_SIM_PYTHON` 可指定带这些扩展的解释器。缺少扩展或解析失败时，读取器明确报告状态和可用的有界元数据，不把回退结果称为完整解析。

依赖探测与单个文件是否成功解析分别记录。安装和配置边界见 [可选依赖](docs/offline-dependencies.md)。本仓库不附扩展依赖二进制、COMSOL 或样例模型。

## STEP/STP

纯 CAD 输入的 `asset_kind` 为 `cad_geometry`，`simulation_state` 为 `not_defined`。物理场、材料、研究、边界条件与网格保持尚未定义；产品名、文件名和实体标签只是元数据，不能指定物理角色。

原生读取统计 STEP 声明与单位信息，不把声明计数当作内核拓扑。可选 Gmsh 路径在有界子进程中读取导入拓扑、边界框和实体体积；未确认的源单位不提升为物理尺寸。体积为导入实体体积之和，不扣除重叠。

快照可包含有界的逐实体摘要和快照范围内的 body ID。body ID、内核 tag 与 COMSOL 域/边界编号不互换。`capabilities` 和 `inspection_limits` 报告已提供及缺少的观察粒度；当前整体/实体观察不能补造逐面定位、最小间隙、壁厚或物理接触。

## MPH

MPH 读取结合可用解析器与 ZIP/XML 保存记录，保留来源、未知项、截断和读取失败。保存节点、参数、选择编码与解文件名不证明有效启用、当前选择成员、结果时效性、网格质量或物理正确性。

原始文件指纹、大小及修改时间用于一致性检查；输入、解压规模、元数据、列表和子进程均有读取上限。具体限制和状态字段以 [model_reader.py](advisor/model_reader.py) 与 [geometry_reader.py](advisor/geometry_reader.py) 为准。

## Deep API

资料库构建可显式选择 COMSOL Deep API 增强。它依赖用户自备且可用的 COMSOL，在工作副本中读取已保存设置；普通 getter 流程不执行几何、网格、默认求解器生成或求解。API 观测与离线保存记录分别保留，差异、缺失和错误不自动判为成功。

几何准备脚本另有显式几何构建步骤，其范围见 [几何挑战说明](scripts/geometry_challenge/README.md)。本说明只描述代码能力，不包含官方模型节点、参数、哈希或现场读取结果。
