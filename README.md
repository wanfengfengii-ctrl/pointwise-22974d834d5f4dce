# 东岬试验中心代码页交换 V1

白班资料员把试验记录器给出的代码和有效毫秒录入设备库。封定后，夜班只需取得完整交换件即可在独立接班库实收。复盘员凭成果号领取各工段的有效时长和原行出处。连续工段核证单在工段账之外，依据随批交接的跨页先后关系，核定目标工段在证据允许的全部原行顺序中的最长连续时长区间。

## 运行

使用 Python 3.11 标准库。控制台物化项目时提供 `check_project.py`。在项目根目录运行完整回归和正常样例。

```sh
python3 -B check_project.py
python3 -B demo.py
```

也可直接发现完整 unittest 套件。

```sh
python3 -B -m unittest discover -s tests -t . -v
```

样例使用两个独立临时数据库。白班将代码 7 对应到「预热」，依次写入 P20 的 11、P03 的 3 和 P20 的 0 毫秒。封定目录为 P20、P03。夜班收到整件后发布「预热」14 毫秒的账，贡献依次为 P20 第 1 行、P20 第 2 行、P03 第 1 行。次批以另一张表记录「稳态」11 毫秒。样例重开接班库并核对两份账及各自出处。核证样例另演示两份预热合计同为 14 毫秒、却分别必达与待查的固定件，一份关系成环被拒签但工段账照常的批，以及无交接记录旧件的缺证处理和重开按原号复领。

## Python 接口

通过 `storage.open_device(path)` 和 `storage.open_receiving(path)` 打开各自的 SQLite 文件。初始化可重复进行。调用方负责关闭连接，可使用 `contextlib.closing`。下列写入入口各自管理事务，调用时连接应无未提交事务。

| 模块与入口 | 结果 |
| --- | --- |
| `tables.register.register_table(db, table_id, entries)` | 固定表号。支持映射或键值对序列。相同映射可重登，同号异义拒绝。 |
| `device.batches.open_batch(db, batch_id, table_id)` | 建立绑定所选固定表的活动批。上一活动批须已封定并签发。 |
| `device.record.append_row(db, batch_id, page_id, code, milliseconds)` | 返回从 1 起算的页内原行位。首条有效行确定页序。 |
| `device.seal.seal_batch(db, batch_id)` | 返回带有固定目录的 `DeviceBatch`。可重封领取同一目录。 |
| `exchange.export.export_batch(db, batch_id)` | 返回封定批的固定交换字节。 |
| `device.relations.register_relation(db, batch_id, before_page, before_line, after_page, after_line)` | 封批前登记一条跨页先后关系，两端须指向活动批已写入的实际原行；只允许跨页，重复登记幂等。 |
| `receiving.accept.receive_batch(db, payload)` | 完整件实收后返回 `Receipt`。同批同件重投复领，同批异件拒绝。 |
| `reporting.publish.publish_report(db, batch_id)` | 给已实收批发布并返回 `SegmentReport`。已发布的同批复领固定成果。 |
| `reporting.read.read_report(db, report_id)` | 领取固定总额和逐行出处。未知成果号抛 `KeyError`。 |
| `certification.apply.apply_certification(db, batch_id, segment, minimum)` | 对已实收批申请连续工段核证单，返回固定 `Certification`。同申请复领同一固定件。 |
| `certification.read.read_certification(db, cert_id)` | 按固定核证号领取核证单。未知核证号抛 `KeyError`。 |

业务输入或状态不合规时抛 `ValueError`。码表代码和行代码须为整数。毫秒须为非负整数，实测零也是原行。空值、布尔值、浮点数和数字字符串不作为整数输入。工段使用登记原文，整数持久化不经过浮点转换。封后不能追加，活动批不能导出。历史表保留，新批选表不影响已发批。

整件以 UTF-8 JSON 承载版本、封定清册、完整固定表和按发页序排列的 Base64 正文。清册固定表摘要及每页长度和原字节摘要。页外层标识包括批、表、页号及发页序，页内正文保存原行位、代码和毫秒。接班库核对齐页及实际收到的原字节，随后按随件表译码。刷新 JSON 外层空白不会改变内容身份。明确的空目录及空正文集合可形成空账。现行整件另含 `relations` 段，承载封批前登记的跨页先后关系（两个原行出处为一组）；缺少该段的旧件仍可实收并签工段账，但核证按缺证处理。关系整段进入交换字节摘要，重投或迟到的关系不会混入异批。

实收回执与译码行一并提交。报告的成果号、工段合计与逐行贡献一并提交。写入失败由 SQLite 回滚，允许重试。工段按首次贡献排序，逐行贡献按发页序和页内原序排列。不同原页即使正文相同也分别计量。设备和接班库均可关闭重开，已发件、实收和历史成果保持固定。

## 连续工段核证单

记录器分写多页时，页内原序可靠，跨页先后只有现场记下的部分关系。封批前用 `register_relation` 登记跨页先后，关系指向实际原行并随批交接；录入顺序、页号和旧账发页序都不能定跨页先后。接班库实收时一并固化关系交接记录。

核证申请指定实收批、目标工段和连续时长下限。算法把每页原行串成页链，把登记关系作为页链之间的额外有向边，构成一个 DAG；关系指向缺失原行抛 `DanglingRelationError`，关系与页链或彼此成环抛 `RelationConflictError`，二者都拒签核证单，但原工段账仍可正常发布。对该 DAG 的每个完整线性扩展（即所有符合证据的完整原行顺序），按原码表译出的工段计算目标工段最长连续时长：相邻同工段时长相加，其他工段的原行即使毫秒为零也会断开；目标工段未出现取零。

核证单返回全部顺序所得最长连续时长的最小值与最大值，各附一份可复算的完整原行顺序（覆盖该批全部原行出处）。结论按门槛判定：最小值达到下限为「必达」，最大值低于下限为「未达」，其余为「待查」。核证号由实收号、工段、门槛经 SHA-256 固定，重开接班库可按原号领取同一固定件。核证异常均为 `ValueError` 子类（`certification.errors`：`MissingEvidenceError`、`DanglingRelationError`、`RelationConflictError`）。支持四页、每页十二条的交错稀疏证据。
