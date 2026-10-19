# dingtalk-to-muse

让 AI 助手读懂你的钉钉消息：作业、截止时间、学校通知，自动分类、自动提醒。

## 问题

- 学校/公司用钉钉发通知，但钉钉**没有个人可用的消息 API**——开放平台要组织管理员权限，普通学生/员工用不上；群机器人只能发不能收；iOS 不允许第三方读取通知。
- 重要通知淹没在群聊里，作业截止日期全靠人脑记，错过就是事故。

## 方案

绕过钉钉 API，从操作系统层面拿消息：

```
钉钉 Mac 版 → macOS 通知中心 → notif_forward.py → notifications.jsonl
                                                        ↓
                                              AI 助手定时拉取 → 分类 → 建提醒
```

- **Mac 端**：每 3 分钟把钉钉通知从系统通知中心数据库快照出来，转成 JSONL（本项目 `notif_dump.py` / `notif_forward.py`）
- **助手端**：每 15 分钟拉取 JSONL，按作业 / 截止时间 / 通知分类，48 小时内的截止事项自动建提醒（见 `muse-integration/`）

## 仓库内容

```
dingtalk-to-muse/
├── notif_dump.py          # 通知中心数据库发现、解析、链路验证（调试用）
├── notif_forward.py       # 定时转发主脚本：SQLite 快照 → JSONL
├── examples/
│   └── com.user.dingtalk-to-muse.plist   # launchd 每 3 分钟运行模板
├── muse-integration/
│   └── README.md          # 助手侧：轮询配置、分类规则、提醒策略
├── README.md
└── LICENSE (MIT)
```

转发脚本本身是通用的（`--app` 参数指定任意应用的 bundle id），钉钉是本仓库的主用例。

## 快速开始（Mac 端）

```bash
# 0. 前提：系统设置 → 隐私与安全性 → 完全磁盘访问权限（给终端/Python）；
#    钉钉 Mac 版通知权限打开

# 1. 验证链路：看最近 20 条通知里有没有钉钉
python3 notif_dump.py --app /Applications/DingTalk.app --limit 20

# 2. 活体验证：给自己发条钉钉 DING，看 120 秒内能不能抓到
python3 notif_dump.py --app com.alibaba.DingTalkMac \
    --watch-seconds 120 --marker "my-test-marker"

# 3. 装定时任务
cp examples/com.user.dingtalk-to-muse.plist ~/Library/LaunchAgents/
# 编辑 plist：改 python 路径、脚本路径后
launchctl load ~/Library/LaunchAgents/com.user.dingtalk-to-muse.plist

# 4. 看结果
tail -f ~/.dingtalk-forward/notifications.jsonl
```

输出格式（JSONL，每行一条）：

```json
{"ts": 1792393468.22, "app": "com.alibaba.DingTalkMac", "title": "李波潮", "body": "实验报告周五前交", "rowid": 774}
```

## 关键技术点

- **通知中心 SQLite 快照**：先 `cp` 数据库 + WAL 到私有 `/tmp` 目录再只读打开，`PRAGMA quick_check` 校验，绝不直连 live DB
- **NSKeyedArchiver 逆向**：通知内容是二进制 plist 序列化对象，解析 `$objects` 图提取 title / body / 时间戳
- **并发安全**：`fcntl` 文件锁 + 原子写入（tmpfile + `os.replace` + 目录 fsync）+ 游标持久化，crash 后不丢不重
- **Loud fail**：数据库被替换、bundle id 变化、游标异常——任何反常直接报错退出，不静默吞掉
- **隐私**：`0700` 数据目录 / `0600` 文件 / 本地轮转日志，无网络请求，数据不出本机
- 纯标准库，零依赖

## 局限（实话）

- 只能采到**进入 macOS 通知中心且轮询时仍存在**的通知；免打扰 / 不弹窗的消息采不到——这是 OS 行为，不是 bug
- 通知中心会淘汰旧记录，轮询间隔别超过 5 分钟
- macOS 大版本升级可能搬数据库路径——`discover()` 按已知位置探测，找不到就 loud fail

## License

MIT
