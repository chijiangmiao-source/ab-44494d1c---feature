# 低温冷却管网巡检审计（中国邮路问题）

巡检机器人必须从指定检修口出发、回到原处，且至少走过每条管段一次。本服务求解无向多重图上的**中国邮路 / 路由巡检问题**：

- 在每条管段均走一次的基础上，选出需要**额外重复**的管段集合，使所有节点度数为偶数；
- 增加总长度**最小**；
- **精确给出全部同优集合数量**（按不同边集合计数，平行管段与等长最短路造成的分解歧义已去重）；
- 按**边标识字典序**生成 `0` 优先规范位向量并确定规范集合；
- 依据全部最优集合把每条管段标为 **必重复 / 可重复 / 从不重复**；
- 为规范集合生成一条从检修口起止、逐步可核对、副本数精确吻合的闭合欧拉路线；
- 支持从一次成功审计启动**可恢复的执行核对**：会话绑定该次路线摘要与检修口，按步提交管段标识 / 方向 / 副本号，后端只推进与下一预期步骤完全一致的连续前缀；游标与操作回执持久化，刷新页面或服务重启后自动恢复。

## 约束与校验

- 2–18 个唯一 ASCII 节点（非空白可打印字符）；
- 1–32 条管段，唯一 ASCII 标识、正整数长度；
- 允许平行管段，禁止自环；
- 检修口必须是已声明节点；
- 断连、未知端点、重复标识、非法长度、不存在的检修口都会返回错误，并在页面定位到具体表/行；
- 审计失败时页面保留输入并清除旧结论。

## 算法要点（`app/solver.py`）

重复边集合是一个 T-join：重复边子图中恰为原奇度节点的节点度数为奇。

1. 全源 Dijkstra 得到奇度节点间最短路距离；
2. 奇度节点子集完美匹配 DP（O(2^k·k²)，k≤18）求最小增程；
3. 以匹配 DP 为门控，逐掩码合成“奇度锚点的一条最短路”与“剩余子问题的最优集合”的**对称差**，用整数掩码天然去重，得到**全部不同的最优 T-join**（小规模实测毫秒级）；
4. 集合按位向量（边标识序、`0` 优先）取最小者为规范集合；按边在全部最优集合中的出现情况分类；
5. Hierholzer 在扩展多重图上生成规范闭合路线，并对同边副本按经过次序编号。

## 本地运行（无 Docker）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
gunicorn -w 2 -b 0.0.0.0:8000 app.server:app
# 浏览器打开 http://localhost:8000
```

## Docker / Compose

```bash
# 构建并启动（宿主机端口可用 HOST_PORT 覆盖，默认 8080）
HOST_PORT=9090 docker compose up -d --build
# 健康检查
curl http://localhost:${HOST_PORT:-8080}/health
```

## 一键核对（verify 单次服务）

名为 `verify` 的服务依次执行：代码测试 → 奇度管网同优分类边界 → 欧拉管网零增程边界 → HTTP 冒烟 → 执行核对流程（幂等 / 游标 / 完成），然后退出，以退出码报告结果。

```bash
# 方式一：编排脚本（包含构建 + 健康等待）
./scripts/verify.sh
HOST_PORT=9090 ./scripts/verify.sh

# 方式二：手动
docker compose build
docker compose up -d web            # 等待 healthy
docker compose run --rm verify      # 退出码 0 表示全部通过
```

不经过容器单独运行核对（会自行拉起临时 gunicorn）：

```bash
python -m app.verify
```

## 测试

```bash
pytest -q
```

- `tests/test_solver.py`：确定性用例 + 60 组随机图对**逐子集暴力枚举 oracle** 的交叉验证（同优数量、分类、规范集、路线）；
- `tests/test_api.py`：HTTP 序列化、错误定位、畸形请求；
- `tests/test_execution.py`：执行核对的会话绑定、幂等重试、异参复用 / 过期游标 / 并发争用拒绝、失败不部分推进、重启持久化恢复与 HTTP 全流程。

## 页面

左侧编辑节点/管段/检修口并发起审计；成功后中部展示总长度、增加长度、同优集合数量、规范位向量与按颜色分类的管网图（力导向布局，平行管段分离绘制），右侧为逐步闭合路线。点击图中管段或标签可高亮其在路线中的**全部经过位置**。

右下方「④ 执行核对」面板从当前审计结论启动执行会话，按步提交管段标识、方向与副本号；页面联动标出**已完成 / 下一步 / 未完成**的路线位置，全部副本按序核对并回到检修口后显示完成。会话进度保存在服务端 SQLite（`PIPE_AUDIT_DB`，默认 `data/execution.db`，compose 挂载卷 `exec_data`），页面刷新、服务重启后自动恢复；重新审计得到不同路线时，旧会话不会附着到新结论。

## API

`POST /api/audit`

```json
{
  "nodes": ["A", "B", "C", "D"],
  "edges": [
    {"id": "e1", "u": "A", "v": "B", "length": 1}
  ],
  "start": "A"
}
```

成功返回 `ok:true`、`auditId`（路线摘要指纹）、`totalLength`、`addedLength`、`optimalCount`、`canonicalVector`、
`canonicalEdges`、每边 `classification`（required/optional/never）、`route`（逐步方向与副本号）
及 `positions`（每条边在路线中的全部步序号）。失败返回 `ok:false`、`error`、`fields`、
`locations`（含表名与行号）。

### 执行核对（可恢复）

`POST /api/execution/start` —— 从一次成功审计启动执行会话：

```json
{
  "opId": "start-m7q2-abc",
  "audit": {"nodes": ["A", "B"], "edges": [{"id": "e1", "u": "A", "v": "B", "length": 1}], "start": "A"}
}
```

服务端重新核算该审计并把会话绑定到其路线摘要与检修口，返回 `sessionId`、`auditId`、`cursor`、`totalSteps`、`nextStep`、`route` 等完整状态。

`POST /api/execution/step` —— 按步推进：

```json
{
  "sessionId": "s…",
  "opId": "step-m7q3-01",
  "expectedCursor": 0,
  "step": {"edgeId": "e1", "from": "A", "to": "B", "copy": 1}
}
```

仅当提交的管段标识、方向、副本号与下一预期步骤**完全一致**且 `expectedCursor` 等于服务端游标时才推进连续前缀。

`GET /api/execution/<sessionId>` —— 查询会话当前状态（页面刷新/服务重启后的恢复入口）。

幂等与一致性约定（启动与推进相同）：

- `opId` 为客户端生成的唯一可打印 ASCII 操作标识（1–64 字符）；**同标识同内容**重试返回首次结果（`replayed:true`，不重复推进）；
- **异参复用**同一标识返回 `op_conflict`，**过期游标**返回 `stale_cursor`，并发标签页争用由单事务序列化后稳定拒绝其一；
- 任何失败都在写入前校验，不会部分推进；游标与操作回执持久化于 SQLite；
- 错误响应（`stale_cursor` / `completed` / `step_mismatch`）附带服务端当前状态，便于页面重新同步。
