# 实体键桥

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 实体键桥 |
| 目录 | `07-keyremap` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10926 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

一对多/多对一、生效窗、旧键只读、重映重放

## 避开

别做成 Feed 冲突归并；正式区不是 staging 换皮

## 互斥切口候选

- 冲突拒收整批 vs 进冲突区后重放
- 废弃旧键只读保留 vs 重写正式键
- 生效窗原子切换 vs 双窗按事件时裁

## 启动（一键）

`powershell
cd projects/07-keyremap
docker compose up -d --build
`

浏览器：http://127.0.0.1:10926/  
健康检查：GET http://127.0.0.1:10926/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10604
python -m pytest -q
`

## 半成功撕裂切断（乙路径）

重映统一走 `POST /api/remap`，口径见 `docs/半成功切断说明.md`（2026-11-20 锁定，当天禁改口）：
入站清除与正式插入在同库 `BEGIN IMMEDIATE` 一次事务内同败同成，缺一方两侧回滚；
同键双路抢映由库级写锁串行，后到只记 `争用`；偏离说明的写法拒写并记 `半成功偏了`。

| 接口 | 用途 |
|------|------|
| `GET /api/inbound` | 入站列表（待重映/已重映） |
| `GET /api/reconciliation` | 互证卡：`torn`/`orphan_cleared` 为空才 `aligned` |
| `GET /api/audits` | 重映留痕：成功/争用/冲突/回滚/半成功偏了 |

撕裂夹具：`fixtures/torn_remap/`（构建器 + 本地 SQLite 库，正式已挂、入站未清、冲突无挂起）。
