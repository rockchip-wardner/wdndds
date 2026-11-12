# AGENTS.md

Quick reference for agent sessions in this repo.

Pythonic wrapper around
[eProsima Fast DDS](https://www.eprosima.com/en/products/all-products/fast-dds).
The bindings are re-exported through a small, ergonomic facade — context
managers, QoS presets, lazy import — so a DDS participant / publisher /
subscriber can be spun up in a few lines. Distribution / import name
`wdndds`; the FastDDS surface lives under `wdndds.dds`.

## 仓库地址与同步策略（2026-11-10）

| 角色 | 地址 | 允许的操作 |
|---|---|---|
| **局域网主仓库** | http://10.10.10.10:8080/wardner | **pull + push** —— 唯一权威源，所有更新的第一站 |
| **GitHub** | https://github.com/rockchip-wardner | **只允许 push** —— 发布镜像，不从它 pull |
| **Gitee 国内镜像** | https://gitee.com/rockchip-wardner | **不允许任何 git 操作** —— 公开只读镜像，内容由上游自动同步 |

远端命名：`origin` = `git@10.10.10.10:wardner/wdndds.git`（局域网），
`github` = `git@github.com:rockchip-wardner/wdndds.git`（只 push）。

**提交顺序不可颠倒：先 `git push origin master`（局域网主仓库），再
`git push github master`（GitHub）。Gitee 一概不碰。** `master` 的跟踪分支指向
`origin`，裸 `git pull` / `git push` 即为对主仓库操作。拉取最新代码只走局域网
主仓库 —— 从 GitHub 拉的是「已发布出去的历史」，会让本地与主仓库分叉。

🚦 **推服务器前必须先征得用户同意（2026-11-12）。**

本地 `git commit` 不用问；**`git push` 一律先问**，用户明确点头再推：

> 「要把 wdndds 的这 N 个提交推到局域网主仓库（10.10.10.10）和 GitHub 吗？」

**不要顺手就推。** 理由：

* 推送是**对外**动作 —— GitHub 是公开镜像，推上去国际可见，撤不回来；
* 用户本地可能还有正在做的改动，先推会把半成品推成「最新」；
* 一次授权只覆盖当次问过的那个仓和那些提交，别顺手推广到别的仓。

顺序本身不变（先局域网、后 GitHub），只是**多了「先问」这一步**。

## 提交信息规范（2026-11-11）

**commit message 必须用英文。**

- 格式：Conventional Commits —— `type(scope): subject`，正文（若有）也用英文。
  例：`feat(dds): expose a QoS preset helper`
- 原因：本仓发布到公开的 GitHub 镜像（发布镜像 = 国际可见），提交历史是给国际读者
  看的 —— 中文提交信息让公开历史没法读。
- **只约束提交信息**：代码注释、docstring、仓内文档（AGENTS.md / README）仍按各仓
  现有的写法（中文没问题）。README 是中英双语，AGENTS.md 允许中文。


## Repo map (one-line each)

- `src/wdndds/` — the facade; `src/wdndds/dds/` — the FastDDS surface.
- `src/wdndds/_build.py` — setuptools `build_py` hook: compiles the C shim and the IDL bindings at install time.
- `cshim_src/` — the C shim sources.
- `idl/` — the DDS IDL definitions.
- `native/` — the prebuilt FastDDS native libraries (`libfastdds.so` / `libfastcdr.so`); `build_native.sh` rebuilds them.
- `include/`, `docs/`, `samples/`, `tests/`.
