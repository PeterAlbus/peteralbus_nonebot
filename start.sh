
# neBot 服务管理脚本 - 支持 start | stop | restart | status | log
# 文件名：manage_nonebot.sh

# 配置区 ==============================================
NB_WORKDIR="/home/PeterAlbus/napcat/nonebot/peteralbus_nonebot"  # NoneBot 项目路径
# VENV_PATH="/root/anaconda3/envs/nonebot"                  # 虚拟环境路径（若无则留空）
PID_FILE="$NB_WORKDIR/nb.pid"              # PID 存储文件
LOG_FILE="$NB_WORKDIR/nb.log"               # 日志文件路径
# ====================================================

# 创建必要的目录和文件
init_files() {
    [ ! -d "$(dirname "$PID_FILE")" ] && mkdir -p "$(dirname "$PID_FILE")"
    [ ! -d "$(dirname "$LOG_FILE")" ] && mkdir -p "$(dirname "$LOG_FILE")"
    touch "$LOG_FILE"
}

# 启动服务
start() {
    # 检查是否已运行
    if [ -f "$PID_FILE" ]; then
        if ps -p $(cat "$PID_FILE") > /dev/null; then
            echo "❌ NoneBot 已在运行 (PID: $(cat "$PID_FILE"))"
            exit 1
        fi
        echo "⚠️  发现旧的 PID 文件，正在清理..."
        rm -f "$PID_FILE"
    fi

    init_files
    cd "$NB_WORKDIR" || exit 1
    
    # 激活虚拟环境（如果配置）
    VENV_CMD=""
    if [ -n "$VENV_PATH" ]; then
        echo "🔧 激活虚拟环境: $VENV_PATH"
        VENV_CMD="conda activate nonebot && "
    fi
    
    echo "🚀 正在启动 NoneBot 服务..."
    nohup bash -c "${VENV_CMD}nb run --reload > \"$LOG_FILE\" 2>&1 & echo \$! > \"$PID_FILE\"" > /dev/null &
    sleep 2
    
    # 验证启动
    if [ -f "$PID_FILE" ] && ps -p $(cat "$PID_FILE") > /dev/null; then
        echo "✅ NoneBot 启动成功! (PID: $(cat "$PID_FILE"))"
        echo "📁 日志文件: $LOG_FILE"
        echo "🔄 使用命令: $0 status 检查运行状态"
    else
        echo "❌ 启动失败！请检查日志: $LOG_FILE"
        exit 1
    fi
}

# 停止服务
stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "⚠️  PID 文件不存在，尝试通过进程名停止..."
        NB_PID=$(pgrep -f "nb run")
    else
        NB_PID=$(cat "$PID_FILE")
    fi
    
    if [ -z "$NB_PID" ]; then
        echo "ℹ️  NoneBot 未运行"
        return
    fi
    
    echo "🛑 正在停止 NoneBot (PID: $NB_PID)..."
    kill -SIGTERM "$NB_PID"
    
    # 等待进程退出
    COUNTER=0
    while ps -p "$NB_PID" > /dev/null && [ $COUNTER -lt 15 ]; do
        sleep 1
        ((COUNTER++))
    done
    
    if ps -p "$NB_PID" > /dev/null; then
        echo "❗强制停止 NoneBot"
        kill -SIGKILL "$NB_PID"
        sleep 1
    fi
    
    # 清理 PID 文件
    rm -f "$PID_FILE"
    echo "✅ NoneBot 已停止"
}

# 重启服务
restart() {
    stop
    sleep 2
    start
}

# 检查状态
status() {
    if [ -f "$PID_FILE" ]; then
        if ps -p $(cat "$PID_FILE") > /dev/null; then
            echo "🟢 NoneBot 正在运行 (PID: $(cat "$PID_FILE"))"
            echo "⏱️  运行时间: $(ps -p $(cat "$PID_FILE") -o etime= | xargs)"
            exit 0
        else
            echo "⚠️  PID 文件存在但进程未运行: $PID_FILE"
        fi
    fi
    
    NB_PID=$(pgrep -f "nb run")
    if [ -n "$NB_PID" ]; then
        echo "🟡 NoneBot 正在运行但未使用此脚本启动 (PID: $NB_PID)"
    else
        echo "🔴 NoneBot 未运行"
    fi
}

# 查看日志
log() {
    [ ! -f "$LOG_FILE" ] && echo "❌ 日志文件不存在: $LOG_FILE" && exit 1
    
    if [ "$1" = "-f" ]; then
        echo "📋 实时追踪日志: $LOG_FILE (Ctrl+C 退出)"
        tail -f "$LOG_FILE"
    else
        echo "📋 显示最后 50 行日志: $LOG_FILE"
        tail -n 50 "$LOG_FILE"
    fi
}

# 使用帮助
usage() {
    echo "用法: $0 {start|stop|restart|status|log}"
    echo "附加命令:"
    echo "  log -f    实时追踪日志"
}

# 主程序 =============================================
case "$1" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    log)
        log "$2"
        ;;
    *)
        usage
        exit 1
esac
exit 0
