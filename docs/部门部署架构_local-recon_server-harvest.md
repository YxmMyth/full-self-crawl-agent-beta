# 部门部署架构 — Local Recon + Server Harvest

> 日期: 2026-05-26
> 状态: **设计共识,暂不实施**(先验证整体能力,等测试稳定再上)

## 形态

```
张三 laptop:                       共享服务器:
  recon openslr.org                ┌─ Mission Queue ────┐
  → 看 catalog 满意                │  张三 / openslr    │
  → submit ───────────────────────→│  李四 / xmind      │
                                   │  王五 / cmap       │
李四 laptop:                       └────────────────────┘
  recon xmind.com                            │
  → submit ───────────────────────→  Worker(s) pull
                                            │
                                    Per-user dir + DB rows
                                    Web 看进度 + 下结果
```

## 为什么 Recon 本地 / Harvest 服务器

| 阶段 | 性质 | 适合的地方 |
|---|---|---|
| **Recon** | 交互重(浏览/登录/human_assist)/ 输出小(catalog 几十 MB + sample 几个 GB)/ 数小时 | **本地** — 人在场,Camoufox headless 也行,login 时人自己导 cookies |
| **Harvest** | 机械批跑(读 catalog → curl 循环 → 写 data)/ 输出大(GB-TB)/ 数小时-天 | **服务器** — 稳网络 + 大磁盘 + 不占 laptop |

Login 这块自然解决 — login 永远是 recon 阶段的事,本地用 Tkinter popup / 手动浏览器导 cookies 都好使,根本不用碰服务器 + Camoufox 那个 headed bug。

## 与 orchestrator 的关系

不冲突,是两层:

```
Orchestrator (已有 D:/XiaoSuData/full-self-crawl-orchestrator):
  需求 → 候选站点排行 → 用户挑哪个站

Mission Runner (本文档定义,还没建):
  用户挑完 + 本地 recon 完 → 提交到服务器跑 harvest
```

Orchestrator 在前(选站),Mission Runner 在后(执行 harvest)。

## 工程化分档

### Lv 0(~2 天,丑但工作)

- 没 web UI,没 queue
- Submit 脚本:`./submit.sh openslr.org 20260526-1120_xxx`
  - 内部 `tar | ssh server "mkdir -p ~/jobs/{user}/{run_id} && tar xz"`
  - 留个 `pending.marker` 文件
- Server cron 每分钟扫 `~/jobs/*/*/pending.marker`
  - 启 harvest → 写 `done.marker` / `failed.marker`
- 看状态:`ssh server ls ~/jobs/{user}/*/`
- 多用户隔离 = Linux 不同 user 账号 + chown
- **适合:3 个人内部用,自己人**

### Lv 1(~1 周,正式但简陋)

- FastAPI:
  - `POST /missions` 提交 run_dir tarball
  - `GET /missions` 看当前队列 + 历史
  - `GET /missions/{id}/logs` 实时日志(SSE / WebSocket)
  - `GET /missions/{id}/download` 拿 data/ tarball
- PostgreSQL `missions` 表(user_id / status / run_id / progress / log_path)
- 一个 worker 进程串行 pull queue
- 简单 HTML 表格前端
- **适合:部门 5-10 人,有 web 界面就行**

### Lv 2(~2-3 周,真生产)

- 多 worker 并行(几个 mission 同时跑)
- 资源 quota(单用户限并发数)
- 用户 login auth(LDAP / SSO / 简单密码)
- Notification(完工邮件 / 钉钉 / 飞书)
- Failed mission 一键 retry
- Data dir 自动归档老的(7 天清)
- **适合:部门 10-50 人长期用**

## Beta 这边要改

### 必改(任何 Lv 都要)

- **Harvest 解耦 DB**:procedural_model 写 disk(recon 写到 `run_dir/wm/procedural_model.md`),harvest 从 disk 读 — 这样 sync 完全只是文件拷贝,DB 不用管
- **加 `--workspace-root` 参数**:harvest 跑在指定目录(为多用户隔离)
- **Camoufox 版本锁定**:本地 + 服务器装同一版本(profile 兼容)

### Lv 1 起要的

- 加 `python -m src.main submit <run_id> --server <url>` CLI 命令
- 进度回报(harvest 跑时 PATCH 状态到 server)

### Lv 2 起要的

- 进程级隔离(每个 mission 一个 worker 子进程 / docker)

## 决策与时序

**不立刻实施。** 当前优先级:

1. **先测试整体能力**(2026-05-26 openslr E2E 是第一步,后续还要测 xmind / cmap / projecteuler 等不同站点形态)
2. 测试中发现的 architecture-level 问题先修(比如 recon planner watchdog、recording agent location dedup、headed launch bug)
3. 等 recon-harvest 流程稳定 → 才上 Lv 0 部署
4. Lv 0 真有人用 1-2 周 → 再决定要不要 Lv 1

**Linus 原则**:做最小可工作的,等真用户骂了再改。别一上来设计 Lv 2,设想中的需求和真实使用 90% 时候不一样。
