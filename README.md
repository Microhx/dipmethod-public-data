# dipmethod-public-data

公开归档 [Dipmethod 出票日历](https://dipmethod.com/calendar/)中的记录买点，以及每只股票在出票当日和下一交易日的一分钟行情、分时图，方便独立核对价格和后续走势。

这里记录的是网站出票数据及第三方行情。记录买点沿用网站 `cost` 原值，不自动视为实际成交均价；分钟行情中出现某个价格不能证明订单成交。项目不提供选股公式、买卖执行规则、仓位或账户收益回测。

## 查看数据

打开 [按日期查看的归档目录](data/README.md)，选择出票日期，再选择股票。每只股票的 `README.md` 展示两天分时图和 CSV 下载链接。图片采用纯白背景、中文标题及说明，标题包含股票名称和代码；红线表示分钟收盘价，绿色虚线表示记录买点，两天使用相同价格坐标范围。缺失分钟处断开，价格数据不做平滑处理；每分钟最高价和最低价保留在 CSV 中。

```text
data/2026-01-05/
├── README.md               当天出票清单和数据状态
├── signals.csv             网站原始出票字段
├── metadata.json           来源、采集时间及汇总状态
└── 002177.SZ/
    ├── README.md           买点、两天分时图、数据链接
    ├── metadata.json       各交易日的行情状态和采集时间
    ├── 2026-01-05.csv      当日一分钟行情
    ├── 2026-01-05.png      当日分时图
    ├── 2026-01-06.csv      下一交易日一分钟行情
    └── 2026-01-06.png      下一交易日分时图
```

首次归档的既往交易日标记为“历史整理”。归档中的采集时间只表示何时取得数据，不能证明历史信号在出票当天已经公开。现有网站接口没有首次公开时间字段，本项目不推算该时间。

## 本地使用

需要 Python 3.10 或更新版本。

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

在本地 `.env` 中配置 `ZHITU_TOKEN`（智兔 API Pro 扩能包权限）和 `TUSHARE_TOKEN`（交易日历权限）。凭证不会写入数据文档或请求日志；`.env`、缓存和 Postman 文件被 Git 忽略。

```sh
# 生成某一个出票日期的完整归档。
python -m dipmethod_public_data --date 2026-01-05

# 处理一段日期内所有已有出票日期。
python -m dipmethod_public_data --start 2026-01-01 --end 2026-01-31

# 首次归档全部已有出票记录；可随时中断后重跑。
python -m dipmethod_public_data --all

# 新增数据和不完整数据会继续获取；完整的历史行情默认复用缓存。
python -m dipmethod_public_data --date 2026-01-05

# 主动重新获取已有行情，以核对行情源更正。
python -m dipmethod_public_data --date 2026-01-05 --refresh

# 仅使用本地 CSV 重画全部图片和更新个股文档，不请求行情接口。
python -m dipmethod_public_data.render

# 仅重画指定出票日期的图片。
python -m dipmethod_public_data.render --date 2026-01-05

# 本地验证，不访问外部接口。
python -m unittest discover -s tests -v

# 核对生成的 CSV、SHA256、文档链接及与本地源快照的一致性。
python -m dipmethod_public_data.verify

# 全量归档完成后，还可以检查是否覆盖源快照中的每条出票。
python -m dipmethod_public_data.verify --require-all
```

不传日期参数时处理网站最新的出票日期。命令结束会输出记录数、行情状态和失败数；存在缺失或不完整行情时返回退出码 `2`，运行配置或上游请求失败返回 `1`。没有后台定时任务，也不会自动提交或推送 GitHub。

中文图表需要系统安装可用中文字体，例如思源黑体、Noto Sans CJK SC、微软雅黑或黑体；缺少字体时会明确报错。

## 发布到 R2 和网站日历

`publish` 将公开图片与日期索引发布到 R2，CSV 链接指向公开 GitHub 仓库中的固定提交。先将归档数据提交并推送到 GitHub，再用 Wrangler 登录并核对账户与目标桶：

```sh
# 生成本地发布计划，不上传；账户 ID 可从 wrangler whoami 查看。
python -m dipmethod_public_data.publish --account-id YOUR_ACCOUNT_ID

# 校验 GitHub 中对应 CSV 的内容，再上传图片，最后发布每日索引。
python -m dipmethod_public_data.publish --account-id YOUR_ACCOUNT_ID --upload

# 新增或重画某个出票日期后，仅发布该日期。
python -m dipmethod_public_data.publish --account-id YOUR_ACCOUNT_ID --date 2026-07-22 --upload

# 默认使用本地 HEAD；也可明确指定已推送的数据提交或分支。
python -m dipmethod_public_data.publish --account-id YOUR_ACCOUNT_ID --github-ref codex/initial-archive --upload
```

默认目标为 `a-stock-images` 桶、`https://cdn.dipmethod.com`，可用 `--bucket` 和 `--cdn` 指定。凭证从本机 Wrangler 登录配置或 `CLOUDFLARE_API_TOKEN` 环境变量读取，不写入发布文件。若登录已过期，先用 Wrangler 刷新登录。

图片位于 `intraday/出票日期/股票代码/行情日期-内容哈希.png`；重画后地址随内容变化，避免旧缓存。CSV 保留在本地和 GitHub，不再上传 R2。索引中的 CSV 引用包含仓库、完整提交 ID、文件路径和 SHA256；发布前会读取该提交的公开 GitHub 文件树，逐份核对 CSV 内容，尚未推送或内容不一致时停止发布，不自动推送 Git。

每日索引为 `intraday/manifests/YYYY-MM-DD.json`，仅在 GitHub CSV 与图片校验完成后更新。重跑会复用 `.cache/r2-publish.json` 中已校验的图片；若远端图片被人为删除，可移走该本地检查点后重新上传和校验。发布命令不会自动删除 R2 中已有的文件。

网站日历按当前所选日期读取索引，再校对信号 ID、日期、代码、名称和记录买点，展示当日与下一交易日分时图。未归档或记录更正后尚未重画的项目会显示说明；本项目未设置自动采集或自动发布任务。

## 字段与数据口径

`signals.csv` 使用 UTF-8 BOM，股票代码保留交易所后缀，字段与网站公开接口一致：

| 字段 | 含义 |
| --- | --- |
| `id`、`seq` | 原始信号 ID、序号 |
| `sig_date` | 出票日期 |
| `code`、`name` | 股票代码、原始名称；缺失代码保留为空 |
| `cost` | 网站记录买点，保留原始数值精度 |
| `peak`、`peak_return` | 网站原有区间最高价、区间涨幅；涨幅为小数原值 |

`peak` 的统计区间可能超过这里公开的两天，不能用两天行情直接验证所有区间涨幅。该项目不会重新计算或补造这两个字段。

行情 CSV 按时间升序，保留智兔原始字段：

| 字段 | 含义 |
| --- | --- |
| `t` | 行情时间标签，北京时间（Asia/Shanghai） |
| `o`、`h`、`l`、`c` | 分钟开盘、最高、最低、收盘价，元，不复权 |
| `v`、`a` | 行情源的成交量、成交额原值；单位尚未独立核实，不用于收益或资金计算 |
| `pc`、`sf` | 行情源附带字段原值，不参与图表和计算 |

信号来自 `https://dipmethod.com/api/signals`，与网站日历使用同一份记录；行情来自智兔 `/hs/pro/history/{code}/1/n`（北交所使用 `/bj/pro/history/{code}/1/n`）；下一交易日按 Tushare `trade_cal` 对应交易所日历确定。

## 缺失、更正与公开范围

- 无代码记录保留在当天清单与 `signals.csv`，不通过名称猜测代码。
- 下一交易日固定按交易日历取值，停牌或无数据时不改用更晚日期。暂无行情不自动判定为停牌。
- 每天检查 09:31–11:30、13:01–15:00 的 240 个常规分钟标签，允许额外的 09:30 标签。缺少标签标记“不完整”，不补值、不插值。行情点齐全也不代表已经独立核实每个价格。
- 当天尚未收盘或下一交易日尚未到来时保留“待补充”状态；每次重跑会重新获取未完成行情。
- 已归档 CSV 发生变化时，原文件保存至 `corrections/`，追加更正日志；文档和图表按最新 CSV 重建。
- 项目本身不授予第三方行情再分发许可。向 GitHub 公开原始行情前，应确认所用行情服务允许该用途。

所有统计仅用于记录核对，历史表现不代表未来表现。
