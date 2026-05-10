#!/usr/bin/env bash
# =============================================================================
# start_vllm_server.sh
# =============================================================================
# 启动 vLLM 推理服务（OpenAI 兼容 API）。
# 用法:
#   ./scripts/start_vllm_server.sh [MODEL_PATH] [PORT] [GPU_IDS]
#
# 示例:
#   ./scripts/start_vllm_server.sh Qwen/Qwen2.5-7B-Instruct 8000 0,1,2,3
#   ./scripts/start_vllm_server.sh /path/to/local/model 8001 0
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# 默认参数
# -----------------------------------------------------------------------------
DEFAULT_MODEL="Qwen/Qwen2.5-7B-Instruct"
DEFAULT_PORT="8000"
DEFAULT_GPUS="0"

MODEL="${1:-$DEFAULT_MODEL}"
PORT="${2:-$DEFAULT_PORT}"
GPUS="${3:-$DEFAULT_GPUS}"

# -----------------------------------------------------------------------------
# 环境检查
# -----------------------------------------------------------------------------
echo "[start_vllm_server] 启动 vLLM 服务"
echo "  模型: ${MODEL}"
echo "  端口: ${PORT}"
echo "  GPU:  ${GPUS}"

if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 python3"
    exit 1
fi

if ! python3 -c "import vllm" 2>/dev/null; then
    echo "错误: 未安装 vllm 包，请先执行: pip install vllm"
    exit 1
fi

# -----------------------------------------------------------------------------
# 启动服务
# -----------------------------------------------------------------------------
# 设置可见 GPU
export CUDA_VISIBLE_DEVICES="${GPUS}"

# 计算 tensor-parallel-size（根据 GPU 数量）
IFS=',' read -ra GPU_ARRAY <<< "${GPUS}"
TP_SIZE="${#GPU_ARRAY[@]}"

echo "[start_vllm_server] Tensor Parallel Size: ${TP_SIZE}"

# 启动 vLLM（后台运行，日志写入文件）
LOG_DIR="logs"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/vllm_server_$(date +%Y%m%d_%H%M%S).log"

echo "[start_vllm_server] 日志文件: ${LOG_FILE}"

# 注意：以下参数可根据模型大小和显存情况调整
python3 -m vllm.entrypoints.openai.api_server \
    --model "${MODEL}" \
    --port "${PORT}" \
    --tensor-parallel-size "${TP_SIZE}" \
    --dtype bfloat16 \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.90 \
    --enforce-eager \
    >> "${LOG_FILE}" 2>&1 &

SERVER_PID=$!
echo "[start_vllm_server] 服务 PID: ${SERVER_PID}"
echo "${SERVER_PID}" > "${LOG_DIR}/vllm_server.pid"

# 等待服务就绪
echo "[start_vllm_server] 等待服务就绪..."
for i in {1..60}; do
    if curl -s "http://localhost:${PORT}/health" > /dev/null 2>&1; then
        echo "[start_vllm_server] 服务已就绪: http://localhost:${PORT}/v1"
        echo "[start_vllm_server] 测试命令:"
        echo "  curl http://localhost:${PORT}/v1/models"
        exit 0
    fi
    sleep 1
done

echo "[start_vllm_server] 警告: 服务启动超时，请检查日志: ${LOG_FILE}"
exit 1
