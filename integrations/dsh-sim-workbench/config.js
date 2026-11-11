import Schema from './vendor/node_modules/@deepseek-ai/schemastery/lib/index.mjs';

/** Official DSH 0.2.2 schema implementation, vendored unchanged for file bundles. */
const path = description => Schema.string().default('').description(description);
export const Config = Schema.object({
  offlineBundle: path('已解压的离线依赖包根目录；保存后自动校验并配置。').volatile(),
  dataDir: path('工作台持久数据目录；留空使用默认目录，切换不会迁移或删除旧数据。').volatile(),
  pythonPath: path('工作台 Python 可执行文件。'),
  docRoot: path('COMSOL 官方手册根目录。'),
  applications: path('COMSOL Application Libraries 根目录。'),
  simPython: path('sim-cli 使用的 Python 可执行文件。'),
  qmd: path('QMD 命令或可执行文件。'),
  qmdDir: path('QMD SDK 目录。'),
  nodePath: path('独立 Node.js 可执行文件。'),
  pageindexPython: path('PageIndex 使用的 Python；可与工作台 Python 相同。'),
  comsolHome: path('COMSOL 安装目录。'),
  startupTimeoutMs: Schema.natural().min(1000).max(120000).default(30000)
    .description('工作台启动超时毫秒数。'),
});
