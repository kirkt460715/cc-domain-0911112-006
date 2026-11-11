# receivables 数据接入工作台

保留现有领域校验、持久化与操作入口。Python 3.11 标准库和 SQLite；运行时无需下载依赖。测试覆盖已有领域行为、错误路径和恢复。本地联调材料不会自动采集到业务库。

## 本地运行

    python3 -m compileall -q receivables
    python3 -m unittest discover -s tests -v
    python3 -m receivables demo

## 容器运行

    docker build -t local/receivables .
    docker run --rm --network none local/receivables

构建时准备基础镜像，正式运行不下载。run_checks.sh 是完整的测试、编译检查及实际产品演示入口，正常在 90 秒内结束。每次演示使用临时数据或内存库，不改写源样本。

## 采集联调

fixtures 中提供目录投递或网页原始样本，复制到临时目录后联调。

## HTML 银行对账单适配器

财务共享渠道只交付 HTML 对账单时，通过版本化适配器接入（`bank_adapter.*` 与
`bank_statement.*` 动作）：

- 适配器配置（表头层级标签、日期格式、跳过行类别/正则、跨行备注与备注抽取规则）
  按版本保存，乐观锁控制变更；
- 按配置展开 `rowspan`/`colspan`、跳过重复页眉，识别并拼接独占下一行的跨行备注；
- 按币种精度解析括号负数、千分位（含全角逗号），映射交易标识、记账日、币种、
  借贷方向与金额；期初余额、合计、分页提示按规则排除；
- 字段缺失、日期不明或歧义、借贷同值、同 ID 内容冲突均携带原表行列位置进入隔离区，
  隔离行不会写入 `bank_lines`；
- 流程为暂存→预览→确认：确认绑定源摘要与适配器版本，预览后规则或流水变化必须重看；
- 业务流水标识由「渠道:账户:交易号」限定，月份、分页或行号不同不会重复入账；
- 出款（debit）与旧的正数入款记录同表兼容，但不进入回款候选、未匹配清单与催收例外。

原有操作参数可通过 python3 -m receivables --help 查询。
