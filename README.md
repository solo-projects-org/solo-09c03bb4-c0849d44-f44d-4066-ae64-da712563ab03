# 港池疏浚验收计量系统

测绘员在网页上沿 1 米格线框选矩形，系统基于前、后两期测深数据核量
**两期共测面积（㎡）** 与 **净挖方（m³，后测减前测，负值照计）**。
后测完成后，还可在网页沿 **8 米格线** 框选选区，导出北向上的
**双波段 GeoTIFF 浅点筛查图** 交 GIS 复核。

## 业务规则

| 规则 | 实现 |
| --- | --- |
| 同期同格取最高测次号 | `readings` 以 `(phase,x,y)` 为主键，低测次号跳过 |
| 空水深也覆盖旧值 | `depth_cm=NULL` 正常 upsert，该格退出计量但保留读数 |
| 同号异值整批退回 | 批内逐格校验，冲突即抛错回滚，一行不写，返回 409 并留痕 |
| 前后都有读数才计量 | 按 `(x,y)` JOIN 两期且两期 `depth_cm` 均非空 |
| 净挖方 | Σ(后测水深 − 前测水深) cm ÷ 100，每格 1 ㎡，负值照计 |
| 框内整块只读汇总 | 完整块直接读 `block_stats` 的配对指标 |
| 切开的块按细格算 | 边缘格回 `readings` 逐格 JOIN |
| 补测只更新受影响块 | 仅重算本批写入格所在块 |
| 读数与统计一起提交 | 同一 SQLite 事务，失败整体回滚，原结果保留 |
| 并发补测不漏算 | 进程写锁 + `BEGIN IMMEDIATE` + WAL，写事务串行 |
| 一次核量取同一份数据 | 整块与边缘查询在同一个只读快照事务内，返回 `data_version` |
| 浅点筛查图导出 | 8 米像元双波段 GeoTIFF，每像元汇总 8×8 后测细格：波段1 最浅有效水深(cm)、波段2 有效细格数 |
| 零水深有效 | `depth_cm=0` 照常参与最浅值与计数；空水深/缺测格不计 |
| 无有效细格的像元 | 两波段均为 NoData（4294967295，GDAL_NODATA 标签） |
| 稀疏大选区 | 边长 ≤ 65536 米（8192×8192 像元）；全空瓦片不写像元数据（offset=0）；逐瓦片生成，内存只留一个瓦片 |
| 导出取同一份数据 | 整个导出在一个只读快照事务内取数，期间补测可继续；文件元数据注明 `data_version` |
| 失败不交付残件 | 先写隐藏临时文件，写完原子改名；失败删除残件，只提供写完的文件 |

## 目录结构

```
app/
  config.py     # 块大小/数据库路径/导出上限等环境变量配置
  database.py   # 连接、建表、WAL、读写事务、数据版本
  ingest.py     # CSV 解析、测次号规则、整批提交/退回、批次留痕
  stats.py      # 块坐标、受影响块重算（含两期配对指标）
  query.py      # 矩形选区：整块汇总 + 边缘细格，同一快照
  export.py     # 浅点筛查图：GeoTIFF 流式写出、稀疏瓦片、导出任务管理
  main.py       # FastAPI 路由与静态网页
web/            # 原生 HTML/JS 画布，沿格线拖选矩形（可吸附 8 米格线导出）
tests/          # unittest 套件（42 个用例，含并发、导出与 HTTP 端到端）
scripts/        # 端到端试算、随机矩形交叉校验、并发压测、导出试算脚本
Dockerfile, docker-compose.yml
```

## 启动方式

### Docker Compose（交付方式）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

数据持久化在命名卷 `dredging-data`（容器内 `/data/dredging.db`，WAL 模式），
导出的 GeoTIFF 在同一卷的 `/data/exports/`。
可用环境变量调整：`BLOCK_SIZE`（默认 16）、`BUSY_TIMEOUT_S`、`DB_PATH`、
`EXPORT_DIR`（默认 `<数据目录>/exports`）、`EXPORT_MAX_SIDE_M`（默认 65536）。

### 本机直接运行

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
DB_PATH=./data/dredging.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## CSV 格式

表头 `x,y,survey_no,depth_cm`（支持中文表头别名：格X/格Y/测次号/厘米水深）。
`depth_cm` 留空表示空水深。每行一个 1 米方格的整数坐标、测次号、厘米水深。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/surveys/{before|after}` | 上传一期 CSV（multipart 字段 `file`） |
| GET | `/api/selection?x0=&y0=&x1=&y1=&cellwise=` | 矩形核量，`cellwise=true` 为全细格校验 |
| GET | `/api/cells` | 矩形内各格前后水深（网页绘图） |
| GET | `/api/extent` | 两期坐标范围 |
| GET | `/api/blocks` | 分块统计全表 |
| GET | `/api/batches` | 批次留痕（含退回原因） |
| POST | `/api/exports` | 创建浅点筛查图导出（JSON `x0,y0,x1,y1`，8 的整数倍，边长 ≤ 65536 米），后台生成 |
| GET | `/api/exports` / `/api/exports/{id}` | 导出任务列表 / 状态轮询（含进度与数据版本） |
| GET | `/api/exports/{id}/file` | 下载写完的 GeoTIFF（未完成返回 409） |
| GET | `/health` | 健康检查与数据版本 |

## GeoTIFF 导出格式

- 像元 8m × 8m，北向上（图像第 0 行 = 最北），坐标沿用港区米制格线
  （x 向东、y 向北；GeoTIFF 记为用户自定义投影坐标系，单位米）。
- 双波段 UInt32，planar separate：波段 1 = 像元对应 8×8 后测细格的最浅
  有效水深（厘米），波段 2 = 有效细格数；两波段共用 NoData = 4294967295。
- 瓦片 256×256，无压缩；全空瓦片不写像元数据（TileOffsets=0 的稀疏瓦片，
  GDAL/QGIS 读取时按 NoData 填充）。
- 元数据（ImageDescription 与 GDALMetadata）注明 `data_version`、选区、
  生成时间与波段含义，文件名同样带数据版本号。

## 测试

```bash
python -m unittest discover -s tests -v          # 42 个用例
python scripts/e2e_trial.py                       # 对运行中服务的 HTTP 试算
python scripts/random_check.py                    # 500 个随机矩形两算法交叉校验
python scripts/concurrency_trial.py               # 1200 次并发补测压测
python scripts/export_trial.py                    # 浅点筛查图导出试算（含零水深/缺测/稀疏选区）
```

## 关键设计说明

- **配对指标入库**：`block_stats` 冗余存 `n_matched`（两期均非空的格数）与
  `diff_cm`（后减前差值和），重算块时用 readings JOIN 得出。不能用两期
  各自总和直接相减——任一期出现空水深/缺测时格集合不一致会算错。
- **同快照核量**：WAL 模式下读不阻塞写；一次核量的整块查询与边缘查询包在
  同一个 `BEGIN` 只读事务里，看到的是事务开始时的已提交版本，响应中带
  `data_version` 可供调用方核对。
- **补测原子性**：读数 upsert、块重算、留痕、版本号递增在一个
  `BEGIN IMMEDIATE` 事务内；任何失败全部回滚。退回批次的留痕用独立事务写入，
  不推进数据版本。
- **导出流式稀疏写出**：不依赖 GDAL，用标准库逐瓦片写 classic TIFF。
  每瓦片的 8×8 细格聚合（`MIN(depth_cm)`、`COUNT(*)`）在 SQL 内完成，
  只返回有有效水深的像元，内存中同时只保留一个瓦片（2 × 256KB）；
  全空瓦片只登记 `offset=0`，8192×8192 像元的稀疏选区也只占极小文件。
  整个导出包在一个只读快照事务里，后台线程生成，期间补测照常提交；
  先写 `.<id>.tmp` 隐藏文件，全部写完（含瓦片索引回填）才 `rename`
  成正式文件，任何失败删除残件。
