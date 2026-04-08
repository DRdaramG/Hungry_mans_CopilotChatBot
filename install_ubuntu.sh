#!/usr/bin/env bash
# ============================================================
#  install_ubuntu.sh
#  Ubuntu / Debian 환경에서 필요한 Python 패키지를 apt로 설치합니다.
#
#  사용법:
#    chmod +x install_ubuntu.sh
#    sudo ./install_ubuntu.sh
# ============================================================

set -euo pipefail

# ── root 권한 확인 ──────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "오류: 이 스크립트는 root 권한이 필요합니다."
    echo "  sudo ./install_ubuntu.sh"
    exit 1
fi

echo "===== Ubuntu/Debian 패키지 설치 시작 ====="

apt-get update

# ── 필수 패키지 목록 ────────────────────────────────────────
PACKAGES=(
    python3-requests          # GitHub 및 Copilot API HTTP 통신
    python3-openpyxl          # .xlsx 엑셀 파일 읽기
    python3-tiktoken          # 컨텍스트 창 관리를 위한 토큰 계산
    python3-typing-extensions # Python 3.11 미만 최신 typing 기능 지원
    python3-tk                # tkinter GUI
)

apt-get install -y "${PACKAGES[@]}"

echo ""
echo "===== 설치 완료 ====="
echo "프로그램을 실행하려면:"
echo "  python3 main.py"
