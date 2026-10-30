# 发布清单（v1.1.0）

## 0. 发布范围

- AstrBot 插件：记忆 / 情绪 / 表达 / 生活层 / 面板
- memory-core：独立记忆服务 + 人格情绪 + 生活层 + 提醒 + 评估 + 3D 面板
- 文档：AstrBot / qqbridge / DSH 三份接入说明

## 1. 发布前检查

- [ ] 工作区干净，`git status` 无未提交文件
- [ ] 插件仓库 74 个测试通过（15 skipped）
- [ ] memory-core 17 个测试通过
- [ ] `pip install .` 在干净虚拟环境通过
- [ ] `memory_core/config.example.json`、`panel.html` 在安装包里
- [ ] 无个人名字、QQ 号、绝对路径、数据库、聊天记录
- [ ] README 链接、版本号、示例命令可执行
- [ ] `examples/openai_client.py` 能跑通
- [x] Windows 干净主机测试：跳过（主机无 Python，VPN 下下载 25MB 安装包只有 ~5MB/几分钟，放弃）

## 2. Windows 测试

主机：`ssh win`（192.168.0.118，dshremote）

- [ ] `python --version` ≥ 3.10
- [ ] 解压 `memory-core-v0.1.0.zip`
- [ ] 建 venv，`pip install .`
- [ ] 启动 `memory-core`，`/health` 200
- [ ] `/panel` 能打开
- [ ] 带 `user` 调 `/v1/chat/completions` 200
- [ ] 不带 `user` 返回 400
- [ ] 提醒 / 生活 / 评估接口各调一次

## 3. 构建产物

不要用本机 AstrBot 的 live 插件构建，会混入下午的房间/玩具改动。

从 `release-v1.1.0` 分支直接打包：

```bash
cd <release-v1.1.0>
zip -qr memory-plugins-v1.1.0.zip plugins -x '*/__pycache__/*' '*.pyc'
zip -qr life-loop-v1.1.0.zip life -x '*/__pycache__/*' '*.pyc' '*/books/*' '*.db*'
zip -qr memory-core-v0.1.0.zip memory-core -x '*/__pycache__/*' '*.pyc' '*/data/*' '*.db*'
```

产物：

```
dist/memory-plugins-v1.1.0.zip
dist/life-loop-v1.1.0.zip
dist/memory-core-v0.1.0.zip
```

## 4. 推送

```bash
git checkout release-v1.1.0
git push origin release-v1.1.0:main
git tag -a v1.1.0 -m "v1.1.0"
git push origin v1.1.0
```

## 5. 发 Release

标题：`v1.1.0 · 记忆、情绪与独立 memory-core`

说明包含：

- 时间召回 / 提醒 / 备份
- 核心人物保护 / 情绪保护 / 惊讶
- 压缩、事实、反思、画像、整理
- memory-core：HTTP / OpenAI / 面板 / 生活层 / 评估
- 三平台接入文档
- 升级说明：AstrBot 直接替换插件；memory-core 新装

附件：

```
memory-plugins-v1.1.0.zip
life-loop-v1.1.0.zip
memory-core-v0.1.0.zip
```

## 6. 发布后验证

- [ ] `git clone` 公开地址
- [ ] 按 docs 安装 AstrBot 插件
- [ ] 按 docs 启动 memory-core
- [ ] `/panel`、`#eval`、`/health` 正常
- [ ] 请求不带 user 返回 400
- [ ] Release 附件可下载

## 7. 回滚

- 删 Release 与 tag
- `git reset --hard 2dd2f2c && git push --force origin main`
- 保留本地 `release-v1.1.0` 分支
