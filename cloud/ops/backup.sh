#!/bin/bash
# 每日题库备份：rsync 硬链接快照，保留 7 天。
#
# 为什么不是每天复制一份全量：
#   --link-dest 让「没改过的文件」在快照里是指向旧快照的**硬链接**（同一份磁盘数据，
#   两个名字）。所以第 2 天的快照只占「当天真正改动过的 md/json」的空间（约 10 MB），
#   而不是 9 GB。7 天加起来约 9.1 GB，不是 63 GB。
#
# 三个必须知道的盲区：
#   ① 备份和原件在同一块盘 —— 防误删、防改错，**防不了磁盘或整机故障**；
#   ② root 的误操作会连备份一起删（比如 rm -rf /opt/qbank）；
#   ③ 没有异地副本时，机器挂了就只能靠本地那台机器上的 data/ 兜底。
set -euo pipefail

D=$(date +%F)
SNAP=/opt/qbank/backup/"$D"
DATA=/opt/qbank/data
LOCK=/tmp/qbank-backup.lock

# 防止两个 cron 叠着跑
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[$(date -Iseconds)] 上一次备份还没结束，跳过"
    exit 0
fi

# 1) 题库快照（--delete 让快照严格等于当前状态；--link-dest 复用未变文件）
PREV=/opt/qbank/backup/latest
if [ -d "$PREV" ]; then
    rsync -a --delete --link-dest="$PREV" "$DATA"/ "$SNAP"/
else
    rsync -a --delete "$DATA"/ "$SNAP"/
fi

# 2) 账号文件单独留一份（不在 data/ 下，不进上面的快照）
cp /opt/qbank/config/users.yaml "/opt/qbank/backup/users-$D.yaml" 2>/dev/null || true

# 2b) 运行状态库 state.db（确认记录 / 有误标记）
#     这是**事实**（重建不出来），必须进备份 —— 和 index.db 正好相反。
#     ⚠️ 不能直接 rsync 一个正在写的 WAL 库（主库与 -wal 分离 → 快照不一致），
#        用 SQLite 自己的 VACUUM INTO 拍原子快照。目标文件必须事先不存在。
STATE=/opt/qbank/var/state.db
if [ -f "$STATE" ]; then
    rm -f "/opt/qbank/backup/state-$D.db"
    if python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("VACUUM INTO ?", (sys.argv[2],)); c.close()' \
        "$STATE" "/opt/qbank/backup/state-$D.db" 2>/dev/null; then
        echo "[$(date -Iseconds)] state.db 快照完成 → state-$D.db"
    else
        echo "[$(date -Iseconds)] ⚠️ state.db 快照失败（确认记录/有误标记未备份）"
    fi
else
    echo "[$(date -Iseconds)] state.db 不存在，跳过（尚未迁移或服务未启动过）"
fi

# 3) 滚动指针 + 清理 7 天前的快照与账号副本
ln -sfn "$SNAP" /opt/qbank/backup/latest
find /opt/qbank/backup -maxdepth 1 -type d -name '20*' -mtime +7 -exec rm -rf {} + 2>/dev/null || true
find /opt/qbank/backup -maxdepth 1 -name 'users-*.yaml' -mtime +7 -delete 2>/dev/null || true
find /opt/qbank/backup -maxdepth 1 -name 'state-*.db' -mtime +7 -delete 2>/dev/null || true

MD=$(find "$SNAP" -name '*.md' | wc -l)
USE=$(du -sh "$SNAP" 2>/dev/null | cut -f1)
TOTAL=$(du -sh /opt/qbank/backup 2>/dev/null | cut -f1)
DISK=$(df -P /opt/qbank | awk 'NR==2 {gsub("%","",$5); print $5}')
echo "[$(date -Iseconds)] snapshot=$SNAP files=$MD 本快照增量后体积=$USE 备份总体积=$TOTAL 磁盘占用=${DISK}%"

# 磁盘红线：85% 就该动手（扩盘或把备份挪到对象存储）
if [ "${DISK:-0}" -ge 85 ]; then
    echo "[$(date -Iseconds)] ⚠️ 磁盘占用 ${DISK}% 已过 85% 红线，请扩容或转移备份"
fi
