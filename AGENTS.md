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

🚦 **「新功能」在用户验收之前不推（2026-11-17 补充）。**

一次授权只覆盖**当次问过的那批仓、那批提交**。特别是**新功能的配套改动**
（这里指：为了某个新卡片才暴露出来的 wire 层修复）：

> 用户在浏览器里看过、点过、确认能用之前 —— **本地 commit 就行，不要推**。

踩过的坑（这次真踩了）：用户上一轮说「提交10和github」，那轮授权覆盖的是
**当次已验证的改动**；下一轮做**新卡片**时，我把那句授权当成了持续授权，
把新卡连同它在 wire 层的修复（TextAttachment 过 DDS）一起推到了局域网与
GitHub —— 用户还没在浏览器里验收。推送是**对外**动作，撤不回来。

⇒ 判断标准：**这次改动用户验收过吗？** 没验收过 ⇒ 停在本地，问一句再推。

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

## Output attachments must be listed explicitly (2026-11-17)

`encode_attachments_out()` only carries the attachment kinds it knows about:
`ImageAttachment` (through `data_path`) and anything with a `.data` payload
(audio). **Anything else is dropped silently** — the caller gets a correct
`text` reply and no attachment, and nothing is logged.

That already bit the image-generation path (the produced image vanished) and
it bit the decision path a second time: `Intern-Decision` returns the full
per-field probabilities and logits in a `TextAttachment`, so every field came
back with probability `0.0` while the yes/no verdict itself was right — which
looks like a model problem instead of a transport problem.

⇒ When a backend starts producing a new kind of output attachment, add it to
`encode_attachments_out()` **and** to the inverse `decode_attachments_out()`
in the same change, and cover the round trip with a test. Unknown keys stay
ignored on the decode side, so an older caller against a newer worker keeps
working.
