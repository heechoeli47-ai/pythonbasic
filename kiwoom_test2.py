# dashboard_app.py
# -*- coding: utf-8 -*-
"""
Kiwoom AutoTrading (Live-ready) - Single File
Conditions -> Top20 -> Pick -> RealReg -> AutoTrade

요약
- 조건검색/TopN 후보를 Trade List로 편입
- 실시간 등록(SetRealReg) + TR 워밍업
- 3분봉 RSI/MACD 기반 자동매매
- Chejan 체결 기반 보유수량/평단/손익 동기화
"""

import csv
import os
import sys
import time
import json
import os
from datetime import datetime
from dataclasses import field
import datetime
from typing import Dict
from dataclasses import dataclass
from typing import List, Optional, Tuple
from PyQt5.QtGui import QColor, QBrush
from PyQt5.QtGui import QFont
import pandas as pd
import pyqtgraph as pg
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QTimer, Qt, QTime
from PyQt5.QtWidgets import (
    QApplication, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QLabel, QComboBox, QSpinBox, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QTextEdit, QGroupBox, QGridLayout, QMessageBox
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# =========================
# Utils
# =========================
def now_str():
    return datetime.datetime.now().strftime("%H:%M:%S")


def clean_code(raw: str) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    return s[1:] if s.startswith("A") else s


def to_int(raw: str, default: int = 0) -> int:
    try:
        if raw is None:
            return default
        s = str(raw).strip().replace(",", "")
        if s == "":
            return default
        return int(float(s))
    except:
        return default


def to_float(raw: str, default: float = 0.0) -> float:
    try:
        if raw is None:
            return default
        s = str(raw).strip().replace(",", "")
        if s == "":
            return default
        return float(s)
    except:
        return default


def ema_series(values: List[float], period: int) -> List[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + (v - out[-1]) * k)
    return out


def macd_series(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """
    returns: (macd_line, signal_line, hist)
    """
    if len(values) < slow + signal + 2:
        return None
    ef = ema_series(values, fast)
    es = ema_series(values, slow)
    m = [a - b for a, b in zip(ef, es)]
    s = ema_series(m, signal)
    h = [a - b for a, b in zip(m, s)]
    return m, s, h


def crossed_down(a: List[float], b: List[float]) -> bool:
    if len(a) < 2 or len(b) < 2:
        return False
    return a[-2] >= b[-2] and a[-1] < b[-1]


def crossed_up(a: List[float], b: List[float]) -> bool:
    if len(a) < 2 or len(b) < 2:
        return False
    return a[-2] <= b[-2] and a[-1] > b[-1]


def rsi_series(values: List[float], period: int = 14) -> List[Optional[float]]:
    """
    RSI series (simple average).
    반환 길이 = values 길이, 초기 구간은 None.
    """
    n = len(values)
    if n == 0:
        return []
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out

    for i in range(period, n):
        gains = 0.0
        losses = 0.0
        for j in range(i - period + 1, i + 1):
            diff = values[j] - values[j - 1]
            if diff >= 0:
                gains += diff
            else:
                losses += -diff
        if losses == 0:
            out[i] = 100.0
        else:
            rs = gains / losses
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out

# =========================
# 보조 로직/구조 정의 구간
# =========================
class HTSAxis(pg.AxisItem):

    def __init__(self, orientation='bottom'):
        super().__init__(orientation=orientation)
        self._dt_list = []

    def set_datetimes(self, dt_list):
        self._dt_list = dt_list or []

    def tickStrings(self, values, scale, spacing):

        out = []

        for v in values:
            i = int(round(v))

            if 0 <= i < len(self._dt_list):
                dt = self._dt_list[i]

                # 일자 변경 지점은 날짜+시간으로 표시
                if i == 0 or dt.date() != self._dt_list[i - 1].date():
                    out.append(dt.strftime("%m/%d %H:%M"))
                else:
                    out.append(dt.strftime("%H:%M"))
            else:
                out.append("")

        return out
# =========================
# Chejan FID (minimum stable set)
# =========================
CHEJAN_FID = {
    "code": 9001,

    # 주문/체결 상태 관련 FID
    "order_status": 913,
    "order_gubun": 905,
    "unfilled_qty": 902,
    "filled_qty": 911,
    "filled_price": 910,

    # 잔고 동기화 관련 FID
    "pos_qty": 930,
    "avg_price": 931,
}


# =========================
# Candles (3s)
# =========================
@dataclass
class Candle:
    start_ts: int
    o: int
    h: int
    l: int
    c: int
    v: int = 0

@dataclass
class DailyLogSummary:
    date: str = field(default_factory=lambda: datetime.datetime.now().strftime("%Y-%m-%d"))
    start_ts: int = field(default_factory=lambda: int(datetime.datetime.now().timestamp()))

    apply_picked_count: int = 0
    realreg_codes_last: str = ""
    realreg_code_count: int = 0

    # TR 수신/컨텍스트 통계 필드
    tr_in_total: int = 0
    tr_opt10001: int = 0
    tr_opt10081: int = 0
    tr_opt10080: int = 0
    tr_ctx_miss: int = 0

    # warmup
    warmup_ready_count: int = 0
    warmup_bars_insufficient: int = 0

    # signal & order
    entry_signals: int = 0
    add_signals: int = 0
    buy_attempts_live: int = 0
    buy_attempts_sim: int = 0
    sell_attempts_live: int = 0
    sell_attempts_sim: int = 0
    buy_block_budget: int = 0

    # errors/warns
    parse_errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # per-code counters
    per_code: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def _bump(self, code: str, key: str, inc: int = 1):
        if not code:
            return
        if code not in self.per_code:
            self.per_code[code] = {}
        self.per_code[code][key] = self.per_code[code].get(key, 0) + inc

    def render(self, arm_live: bool, today_spent: int, daily_budget: int) -> str:
        uptime = int(datetime.datetime.now().timestamp()) - self.start_ts
        hh = uptime // 3600
        mm = (uptime % 3600) // 60

        lines = []
        lines.append(f"===== DAILY OBSERVER SUMMARY ({self.date}) =====")
        lines.append(f"Uptime: {hh:02d}:{mm:02d} | ARM_LIVE={'ON' if arm_live else 'OFF'}")
        lines.append("")
        lines.append("[Setup]")
        lines.append(f"- Apply Picked: {self.apply_picked_count} time(s)")
        lines.append(f"- RealReg: {self.realreg_code_count} code(s)")
        if self.realreg_codes_last:
            lines.append(f"- RealReg last codes: {self.realreg_codes_last[:120]}{'...' if len(self.realreg_codes_last)>120 else ''}")
        lines.append("")
        lines.append("[TR Health]")
        lines.append(f"- TR IN total: {self.tr_in_total}")
        lines.append(f"  - opt10001: {self.tr_opt10001}")
        lines.append(f"  - opt10081: {self.tr_opt10081}")
        lines.append(f"  - opt10080: {self.tr_opt10080}")
        lines.append(f"- ctx-miss: {self.tr_ctx_miss}")
        lines.append("")
        lines.append("[Warmup/Indicators]")
        lines.append(f"- warmup_ready: {self.warmup_ready_count}")
        lines.append(f"- bars insufficient(<25): {self.warmup_bars_insufficient}")
        lines.append("")
        lines.append("[Signals/Orders]")
        lines.append(f"- ENTRY signals: {self.entry_signals} | ADD signals: {self.add_signals}")
        lines.append(f"- BUY attempts: LIVE={self.buy_attempts_live}, SIM={self.buy_attempts_sim}")
        lines.append(f"- SELL attempts: LIVE={self.sell_attempts_live}, SIM={self.sell_attempts_sim}")
        lines.append(f"- BUY budget blocks: {self.buy_block_budget}")
        lines.append(f"- Budget: spent={today_spent:,} / budget={daily_budget:,}")
        lines.append("")
        lines.append("[Per-code highlights]")
        # 코드별 통계는 상위 일부만 요약 출력
        for code, stats in list(self.per_code.items())[:8]:
            s = ", ".join([f"{k}:{v}" for k, v in stats.items()])
            lines.append(f"- {code}: {s}")
        if len(self.per_code) > 8:
            lines.append(f"- ... ({len(self.per_code)-8} more codes)")
        lines.append("")
        if self.warnings:
            lines.append("[Warnings]")
            lines.extend([f"- {w}" for w in self.warnings[-10:]])
            lines.append("")
        if self.parse_errors:
            lines.append("[Errors]")
            lines.extend([f"- {e}" for e in self.parse_errors[-10:]])
            lines.append("")
        lines.append("===== END =====")
        return "\n".join(lines)


# =========================
# Trade item
# =========================
@dataclass
class TradeItem:
    code: str
    name: str = ""
    last: int = 0
    vol: int = 0
    prev_o: int = 0
    prev_c: int = 0
    today_o: int = 0  # 당일 시가(당일 캔들 판정용)
    pos_qty: int = 0
    avg_price: float = 0.0
    pending_sell: bool = False  # 중복 매도 주문 방지 플래그
    pending_buy: bool = False  # 중복 매수 주문 방지 플래그
    synced_from_balance: bool = False  # 잔고조회로 동기화된 종목 여부
    manual_picked: bool = False
    auto_created_by_chejan: bool = False

    # 전략 단계/신호 상태 관리 변수
    stage: int = 0
    signal: str = "WAIT"
    used_budget: int = 0
    daily_entry_count: int = 0
    # 3초봉 실시간 버퍼
    cur_3s: Optional[Candle] = None
    candles_3s: List[Candle] = field(default_factory=list)
    closes_3s: List[float] = field(default_factory=list)

    take_profit1_done: bool = False
    last_tp1_bucket: int = -1
    last_exit_bucket: int = -1
    highest_price: float = 0.0
    # 차트 표시용 3분봉 시계열
    closes_3m: List[float] = field(default_factory=list)
    times_3m: List[int] = field(default_factory=list)  # epoch seconds

    # 지표 계산용 3분봉 시계열
    closes_3m_calc: List[float] = field(default_factory=list)
    times_3m_calc: List[int] = field(default_factory=list)  # epoch seconds
    cur_3m_bucket: int = 0
    cur_3m_close: float = 0.0

    # 중복 신호 방지 버킷 타임스탬프
    last_entry_bucket: int = -1
    last_exit_bucket: int = 0

    # 손절/재진입 대기 상태 변수
    stop1_done: bool = False
    stop1_wait_bars: int = 0
    stop_day: str = ""  # YYYYMMDD
    last_eod_date: str = ""  # EOD 청산 체크를 수행한 마지막 날짜
    # RSI 이력 및 저점 추적 버퍼
    rsi_hist_3m: List[float] = field(default_factory=list)
    rsi_min_after_buy1: float = 999.0  # 1차 진입 후 관찰된 RSI 최저값

    realized_pnl_today: int = 0  # 당일 실현손익 누적
    last_fill_px: int = 0  # 최근 체결가 캐시

    rsi_was_below: bool = False
    prev_rsi_last: float = 0.0

# =========================
# Main App
# =========================
class AutoTrader(QWidget):
    # ARM OFF 시 모의 체결 적용 옵션
    SIM_FILL_WHEN_ARM_OFF = True

    def __init__(self):
        super().__init__()
        self.logs = []  # 메모리 로그 버퍼
        self.txt_logs = None  # 우측 로그 텍스트 위젯 참조

        self.setWindowTitle("Kiwoom AutoTrading (Live-ready) - FINAL INTEGRATED")
        self.setMinimumSize(1200, 850)
        self.resize(1300, 880)

        self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        self._connect_signals()
        self.log("[SIGNALS] connected (OnEventConnect/Condition/Tr/Real/Chejan)")

        # 초기화 설정 구간
        self.is_warmup = True  # 장초반 워밍업 상태
        self.auto_trade_enabled = False  # 자동매매 ON/OFF 상태
        self._summary_done = False
        self.market_open_time = datetime.time(9, 0)

        # 종목별 워밍업 완료 상태 맵
        self.warmup_ready = {}  # {code: True/False}
        self.daily_sum = DailyLogSummary()

        # status
        self.connected = False
        self.arm_live = False
        self.auto_trade_enabled = False

        # budget
        self.daily_budget = 0
        self.split_1 = 50
        self.split_2 = 50
        self.today_spent = 0
        self.today_total_spent = 0
        self.today_total_realized = 0
        self.daily_loss_limit_pct = 2.0
        self.daily_loss_hit = False
        self._risk_log_ts: Dict[str, float] = {}
        self.regime_filter_mode = "AUTO"   # OFF / AUTO / TREND_ONLY / MEANREV_ONLY
        self.market_regime = "UNKNOWN"
        self.market_regime_breadth = 0.5
        self.slippage_bps_buy = 8
        self.slippage_bps_sell = 8
        # 기술지표 기본 파라미터
        self.rsi_period = 14
        self.rsi_buy_level = 35  # RSI 매수 기준값
        self.rsi_sell_level = 75  # RSI 매도 기준값
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal = 9

        # 추가매수 조건 파라미터
        self.add_buy_rsi_level = 24  # 추가매수 RSI 기준값
        self.add_buy_drop_pct = 0.02  # 추가매수 하락률 기준

        # candles settings
        self.candle_sec = 3
        self.max_candles_3s = 1200  # 3초봉 최대 보관 개수
        self.max_bars_3m = 600  # 3분봉 최대 보관 개수

        # TR mgmt
        self.tr_wait: Dict[str, Dict] = {}
        self.screen_real = "5000"
        self.screen_tr = "6000"
        self.screen_order = "8000"

        # data
        self.conditions: List[Tuple[int, str]] = []
        self.candidates: Dict[str, Dict] = {}
        self.trade_map: Dict[str, TradeItem] = {}
        self.display_code: Optional[str] = None
        # ===== Performance Engine =====
        self.perf_today = {
            "buy": 0,
            "sell": 0,
            "realized": 0,
            "trades": 0
        }

        # 기본정보 TR 큐와 타이머 초기화
        self.basic_q: List[str] = []
        self.basic_timer = QTimer(self)
        self.basic_timer.timeout.connect(self._drain_basic_q)
        self.basic_timer.start(80)  # 기본정보 TR 큐 처리 타이머 시작(80ms)
        self.apply_tr_q = []
        self.apply_tr_timer = QTimer(self)
        self.apply_tr_timer.timeout.connect(self._drain_apply_tr_q)

        self._build_ui()
        self._connect_signals()
        self._apply_config()

        # 전략 루프 타이머 설정
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_strategy_tick)
        self.timer.start(500)

        # TR housekeeping
        self.timer_tr = QTimer(self)
        self.timer_tr.timeout.connect(self._tr_housekeeping)
        self.timer_tr.start(30000)
        self.sell1_done = False
        # 장 상태 감시 타이머 설정
        self.market_timer = QTimer(self)
        self.market_timer.timeout.connect(self.check_market_open)
        self.market_timer.start(1000)  # 장 상태 감시 타이머 시작(1초)

        # 당일 요약 복원 후 계좌 요약 갱신
        self._load_today_summary()
        self._update_account_summary_program()

    def _on_event_connect(self, err_code):
        err_code = int(err_code)
        if err_code == 0:
            self.connected = True
            # 로그인 직후 조건식 로딩 예약
            QTimer.singleShot(1000, self._load_conditions)
            self.log("[COND] scheduled GetConditionLoad after login (1s)")

            self.log("[LOGIN] connected OK")
            self._set_status_connected_ui(True)
            self._load_accounts_to_combo()
            QTimer.singleShot(2000, self._request_balance_opw00018)
        else:
            self.connected = False
            self.log(f"[LOGIN] failed err={err_code}")
            self._set_status_connected_ui(False)

    # -------------------------
    # UI
    # -------------------------
    def _build_ui(self):
        # =========================
        # UI 레이아웃 구성 구간
        # =========================
        from PyQt5.QtGui import QFont
        base_font = QFont()
        base_font.setPointSize(11)  # UI 기본 글꼴 크기
        self.setFont(base_font)

        DEBUG_UI_CLICKS = True  # 버튼 클릭 디버그 로그 활성화

        root = QVBoxLayout()

        # =========================
        # UI 레이아웃 구성 구간
        # =========================
        top1 = QHBoxLayout()

        self.lbl_status = QLabel("Status: DISCONNECTED")
        self.lbl_status.setStyleSheet("font-weight:bold;")

        self.btn_login = QPushButton("Connect/Login")
        self.btn_arm = QPushButton("ARM LIVE (실주문) OFF")

        self.cmb_account = QComboBox()
        self.cmb_account.setMinimumWidth(160)

        top1.addWidget(self.lbl_status)
        top1.addSpacing(6)
        top1.addWidget(self.btn_login)
        top1.addWidget(self.btn_arm)
        top1.addSpacing(6)
        top1.addWidget(QLabel("Account"))
        top1.addWidget(self.cmb_account)
        top1.addStretch(1)

        # =========================
        # UI 레이아웃 구성 구간
        # =========================
        top2 = QHBoxLayout()

        self.btn_refresh_bal = QPushButton("Refresh Balance(TR)")
        self.btn_load_cond = QPushButton("Load Conditions")
        self.cmb_cond = QComboBox()
        self.cmb_cond.setMinimumWidth(240)
        self.btn_run_cond = QPushButton("Selected Condition -> Cand")

        self.spn_topn = QSpinBox()
        self.spn_topn.setRange(1, 200)
        self.spn_topn.setValue(20)

        self.spn_pickn = QSpinBox()
        self.spn_pickn.setRange(1, 50)
        self.spn_pickn.setValue(5)

        self.btn_pick_checked = QPushButton("Pick.C -> Tr.L")
        self.btn_remove_trade_selected = QPushButton("Tr.L Remove Selected")
        self.btn_keep_holdings_only = QPushButton("Tr.L Keep Holdings")
        self.btn_set_real = QPushButton("(.P -> SetRealReg + TR(P))")
        self.btn_auto_trade = QPushButton("Auto Trade OFF")

        top2.addWidget(self.btn_refresh_bal)
        top2.addSpacing(6)
        top2.addWidget(self.btn_load_cond)
        top2.addWidget(self.cmb_cond)
        top2.addWidget(self.btn_run_cond)
        top2.addSpacing(10)
        top2.addWidget(QLabel("TopN"))
        top2.addWidget(self.spn_topn)
        top2.addWidget(QLabel("PickN"))
        top2.addWidget(self.spn_pickn)
        top2.addSpacing(10)
        top2.addWidget(self.btn_pick_checked)
        top2.addWidget(self.btn_remove_trade_selected)
        top2.addWidget(self.btn_keep_holdings_only)
        top2.addWidget(self.btn_set_real)
        top2.addWidget(self.btn_auto_trade)
        top2.addStretch(1)

        # =========================
        # CONFIG + SUMMARY ROW
        # =========================
        config_row = QHBoxLayout()

        # 전략/리스크 설정 패널 구성
        config_group = QGroupBox("Config")
        cg = QGridLayout()

        # 설정 입력 위젯 초기값 지정
        self.ed_budget = QLineEdit("100000")
        self.ed_split1 = QSpinBox();
        self.ed_split1.setRange(0, 100);
        self.ed_split1.setValue(50)
        self.ed_split2 = QSpinBox();
        self.ed_split2.setRange(0, 100);
        self.ed_split2.setValue(50)

        self.ed_macd_fast = QSpinBox();
        self.ed_macd_fast.setRange(2, 100);
        self.ed_macd_fast.setValue(12)
        self.ed_macd_slow = QSpinBox();
        self.ed_macd_slow.setRange(2, 200);
        self.ed_macd_slow.setValue(26)
        self.ed_macd_sig = QSpinBox();
        self.ed_macd_sig.setRange(2, 100);
        self.ed_macd_sig.setValue(9)

        self.ed_rsi_buy = QLineEdit("35")
        self.ed_daily_loss_pct = QLineEdit("2.0")
        self.ed_slippage_buy_bps = QLineEdit("8")
        self.ed_slippage_sell_bps = QLineEdit("8")
        self.cmb_regime_filter = QComboBox()
        self.cmb_regime_filter.addItems(["AUTO", "OFF", "TREND_ONLY", "MEANREV_ONLY"])
        self.btn_apply_cfg = QPushButton("Apply Config")

        self.lbl_budget_hint = QLabel("Budget Hint: PerStock=0 (1st=0, 2nd=0) | today_spent=0")
        self.lbl_budget_hint.setStyleSheet("color: #2c7; font-weight:bold;")

        r = 0
        cg.addWidget(QLabel("Daily Budget (KRW)"), r, 0);
        cg.addWidget(self.ed_budget, r, 1);
        r += 1
        cg.addWidget(QLabel("Split 1st (%)"), r, 0);
        cg.addWidget(self.ed_split1, r, 1);
        r += 1
        cg.addWidget(QLabel("Split 2nd (%)"), r, 0);
        cg.addWidget(self.ed_split2, r, 1);
        r += 1
        cg.addWidget(QLabel("MACD Fast"), r, 0);
        cg.addWidget(self.ed_macd_fast, r, 1);
        r += 1
        cg.addWidget(QLabel("MACD Slow"), r, 0);
        cg.addWidget(self.ed_macd_slow, r, 1);
        r += 1
        cg.addWidget(QLabel("MACD Signal"), r, 0);
        cg.addWidget(self.ed_macd_sig, r, 1);
        r += 1
        cg.addWidget(QLabel("RSI Buy Level"), r, 0);
        cg.addWidget(self.ed_rsi_buy, r, 1);
        r += 1
        cg.addWidget(QLabel("Daily Loss Limit (%)"), r, 0);
        cg.addWidget(self.ed_daily_loss_pct, r, 1);
        r += 1
        cg.addWidget(QLabel("Regime Filter"), r, 0);
        cg.addWidget(self.cmb_regime_filter, r, 1);
        r += 1
        cg.addWidget(QLabel("Buy Slippage (bps)"), r, 0);
        cg.addWidget(self.ed_slippage_buy_bps, r, 1);
        r += 1
        cg.addWidget(QLabel("Sell Slippage (bps)"), r, 0);
        cg.addWidget(self.ed_slippage_sell_bps, r, 1);
        r += 1

        cg.addWidget(self.btn_apply_cfg, r, 0, 1, 2);
        r += 1
        cg.addWidget(self.lbl_budget_hint, r, 0, 1, 2);
        r += 1

        config_group.setLayout(cg)

        # 계좌 요약 패널 구성
        summary_group = QGroupBox("Account Summary")
        sg = QGridLayout()

        self.lbl_total_buy = QLabel("총매입: 0")
        self.lbl_total_eval = QLabel("총평가: 0")
        self.lbl_total_profit = QLabel("총손익: 0")
        self.lbl_total_profit_rate = QLabel("수익률: 0%")

        for w in [self.lbl_total_buy, self.lbl_total_eval, self.lbl_total_profit, self.lbl_total_profit_rate]:
            w.setStyleSheet("font-weight:bold;")

        sg.addWidget(self.lbl_total_buy, 0, 0)
        sg.addWidget(self.lbl_total_eval, 1, 0)
        sg.addWidget(self.lbl_total_profit, 2, 0)
        sg.addWidget(self.lbl_total_profit_rate, 3, 0)

        summary_group.setLayout(sg)
        summary_group.setMinimumWidth(260)

        config_row.addWidget(config_group, 1)
        config_row.addWidget(summary_group, 0)

        # =========================
        # Candidates Table
        # =========================
        self.tbl_candidates = QTableWidget(0, 7)
        self.tbl_candidates.setHorizontalHeaderLabels(
            ["Pick", "Code", "Name", "Price", "Vol", "Value", "Note"]
        )
        self.tbl_candidates.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_candidates.verticalHeader().setVisible(False)

        cand_group = QGroupBox("Candidates (조건검색 결과 + TopN 정렬 / 체크해서 Pick)")
        cand_layout = QVBoxLayout()
        cand_layout.addWidget(self.tbl_candidates)
        cand_group.setLayout(cand_layout)

        # =========================
        # Trade List Table
        # =========================
        self.tbl_trade = QTableWidget(0, 14)
        self.tbl_trade.setHorizontalHeaderLabels([
            "Code", "Name", "Last", "Vol", "Signal", "PrevO", "PrevC", "TodayO",
            "PosQty", "AvgPrice", "EvalPnL", "PnL(%)", "MACD(3m)", "RSI(3m)"
        ])
        self.tbl_trade.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_trade.verticalHeader().setVisible(False)

        trade_group = QGroupBox("Trade List (Picked)  ※ 클릭하면 우측 차트 종목 변경")
        trade_layout = QVBoxLayout()
        trade_layout.addWidget(self.tbl_trade)
        trade_group.setLayout(trade_layout)
        self.tbl_trade.cellClicked.connect(self._on_trade_row_clicked)

        # =========================
        # RIGHT SIDE (Charts + Logs)
        # =========================
        mid = QHBoxLayout()

        left_col = QVBoxLayout()
        left_col.addLayout(top1)
        left_col.addLayout(top2)
        left_col.addLayout(config_row)
        left_col.addWidget(cand_group, 3)
        left_col.addWidget(trade_group, 2)

        right_col = QVBoxLayout()

        # =========================
        # UI 레이아웃 구성 구간
        # =========================
        self.chart_widget = pg.GraphicsLayoutWidget()
        self.chart_widget.setBackground("k")

        right_col.addWidget(self.chart_widget, 3)

        # 차트 객체 초기화
        self._ensure_charts()
        # ---- Logs
        if not hasattr(self, "txt_logs") or self.txt_logs is None:
            self.txt_logs = QTextEdit()
        self.txt_logs.setReadOnly(True)
        right_col.addWidget(self.txt_logs, 1)

        mid.addLayout(left_col, 3)
        mid.addLayout(right_col, 2)

        root.addLayout(mid)
        self.setLayout(root)


        # =========================
        # [FINAL] Button binds (one-time, no router)
        # =========================
        def _bind_clicked(btn, handler, tag):
            # 기존 클릭 핸들러 중복 연결 제거
            try:
                btn.clicked.disconnect()
            except Exception:
                pass

            # 디버그 클릭 로그 핸들러 연결
            if 'DEBUG_UI_CLICKS' in globals() and DEBUG_UI_CLICKS:
                btn.clicked.connect(lambda *a, **k: self.log(f"[UI] {tag} clicked"))

            # 실제 버튼 핸들러 연결
            btn.clicked.connect(handler)
            self.log(f"[UI] bind OK: {tag} -> {handler.__name__}")

        # -------------------------
        # 버튼 이벤트 바인딩 처리
        # -------------------------
        _bind_clicked(self.btn_login, self._on_click_login, "btn_login")
        _bind_clicked(self.btn_load_cond, self._load_conditions, "btn_load_cond")
        _bind_clicked(self.btn_run_cond, self._run_condition_to_candidates, "btn_run_cond")

        # 버튼 이벤트 바인딩 처리
        # 잔고조회 버튼 바인딩
        _bind_clicked(self.btn_refresh_bal, self._request_balance_opw00018, "btn_refresh_bal")

        # 설정 적용 버튼 바인딩
        _bind_clicked(self.btn_apply_cfg, self._apply_config, "btn_apply_cfg")

        # -------------------------
        # 버튼 이벤트 바인딩 처리
        # 버튼 이벤트 바인딩 처리
        # -------------------------
        _bind_clicked(self.btn_pick_checked, self._on_pick_checked, "btn_pick_checked")
        _bind_clicked(self.btn_remove_trade_selected, self._on_remove_selected_trade, "btn_remove_trade_selected")
        _bind_clicked(self.btn_keep_holdings_only, self._on_keep_holdings_only, "btn_keep_holdings_only")
        _bind_clicked(self.btn_set_real, self._on_setrealreg_and_tr_picked, "btn_set_real")
        _bind_clicked(self.btn_auto_trade, self._on_toggle_auto_trade, "btn_auto_trade")
        _bind_clicked(self.btn_arm, self._on_toggle_arm_live, "btn_arm")

    def _on_trade_row_clicked(self, row, col):
        """Trade List 클릭 시 우측 차트 종목 변경"""
        item = self.tbl_trade.item(row, 0)
        if not item:
            return

        code = item.text().strip()
        if not code:
            return

        self.display_code = code
        self.log(f"[CHART] display_code set to {code}")

        self._refresh_charts()

    def _make_trade_code_item(self, code: str, checked: bool = False) -> QTableWidgetItem:
        it = QTableWidgetItem(str(code))
        it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        return it

    def _set_account_summary(self, total_buy: int, total_eval: int, total_profit: int):
        # 요약 라벨 안전 갱신
        if hasattr(self, "lbl_total_buy"):
            self.lbl_total_buy.setText(f"총매입: {total_buy:,}")
        if hasattr(self, "lbl_total_eval"):
            self.lbl_total_eval.setText(f"총평가: {total_eval:,}")

        # 수익률 계산
        rate = 0.0
        if total_buy > 0:
            rate = (total_profit / total_buy) * 100.0

        if hasattr(self, "lbl_total_profit_rate"):
            self.lbl_total_profit_rate.setText(f"수익률: {rate:.2f}%")

        # 손익 부호에 따라 색상 결정
        color = "red" if total_profit > 0 else ("blue" if total_profit < 0 else "gray")
        style = f"font-weight:bold; color:{color};"
        if hasattr(self, "lbl_total_profit"):
            self.lbl_total_profit.setStyleSheet(style)
        if hasattr(self, "lbl_total_profit_rate"):
            self.lbl_total_profit_rate.setStyleSheet(style)

        # 계좌 요약 라벨 갱신
        if hasattr(self, "lbl_top_profit"):
            self.lbl_top_profit.setText(f"총손익: {total_profit:,}")
            self.lbl_top_profit.setStyleSheet(style)

    def _set_trade_pnl_color(self, row: int, eval_pnl: int, pnl_pct: float):
        # 손익 색상 표시 처리
        if eval_pnl > 0:
            color = QColor(220, 50, 50)  # red-ish
        elif eval_pnl < 0:
            color = QColor(50, 90, 220)  # blue-ish
        else:
            color = QColor(160, 160, 160)  # gray

        for col in (10, 11):  # EvalPnL, PnL(%)
            item = self.tbl_trade.item(row, col)
            if item:
                item.setForeground(QBrush(color))

    def _calc_eval_pnl(self, t):
        """
        실시간 평가손익/수익률(%) 계산.
        - 수수료/세금 미반영
        """
        if not t or t.pos_qty <= 0:
            return 0, 0.0
        if t.last <= 0 or t.avg_price <= 0:
            return 0, 0.0

        eval_pnl = int((t.last - t.avg_price) * t.pos_qty)
        pnl_pct = ((t.last / t.avg_price) - 1.0) * 100.0
        return eval_pnl, pnl_pct

    def _recalc_per_stock_budget(self):
        """
        PerStock 예산은 실제 Trade List(=trade_map) 종목 수 기준으로 계산.
        PickN은 후보 선택용이며 예산 분배 계산에는 직접 반영하지 않음.
        """
        daily = int(getattr(self, "daily_budget", 0) or 0)
        actual_n = len(getattr(self, "trade_map", {}) or {})

        if daily <= 0 or actual_n <= 0:
            self.per_stock_budget = 0
            return

        self.per_stock_budget = daily // actual_n

    def _style_button(self, btn, bg="#2ecc71", fg="black"):
        btn.setStyleSheet(f"""
            QPushButton {{
                font-size: 14px;
                font-weight: bold;
                background-color: {bg};
                color: {fg};
                padding: 6px 10px;
                border-radius: 6px;
            }}
            QPushButton:hover {{
                background-color: #27ae60;
            }}
            QPushButton:pressed {{
                background-color: #1e8449;
            }}
            QPushButton:disabled {{
                background-color: #d0d0d0;
                color: #777777;
            }}
        """)

    def _apply_button_styles(self):
        # top bar
        self._style_button(self.btn_login, "#3498db")  # 로그인 버튼 스타일
        self._style_button(self.btn_arm, "#9b59b6")  # ARM 버튼 스타일
        self._style_button(self.btn_load_cond, "#7f8c8d")  # 조건 로드 버튼 스타일
        self._style_button(self.btn_run_cond, "#7f8c8d")  # 조건 실행 버튼 스타일

        self._style_button(self.btn_pick_checked, "#2ecc71")  # green: pick
        if hasattr(self, "btn_remove_trade_selected"):
            self._style_button(self.btn_remove_trade_selected, "#e67e22")
        if hasattr(self, "btn_keep_holdings_only"):
            self._style_button(self.btn_keep_holdings_only, "#16a085")
        self._style_button(self.btn_set_real, "#3498db")  # blue: apply+realreg
        self._style_button(self.btn_auto_trade, "#f1c40f")  # yellow: autotrade toggle

        # config
        self._style_button(self.btn_apply_cfg, "#2ecc71")  # 설정 적용 버튼 스타일

    # Signals
    # -------------------------
    def _connect_signals(self):
        self.ocx.OnEventConnect.connect(self._on_event_connect)
        self.ocx.OnReceiveConditionVer.connect(self._on_receive_condition_ver)
        self.ocx.OnReceiveTrCondition.connect(self._on_receive_tr_condition)
        self.ocx.OnReceiveTrData.connect(self._on_receive_tr_data)
        self.ocx.OnReceiveRealData.connect(self._on_receive_real_data)
        self.ocx.OnReceiveChejanData.connect(self._on_receive_chejan_data)

    def _set_label_pnl_color(self, eval_pnl: int):
        # 손익 색상 적용
        if eval_pnl > 0:
            css = "color: rgb(220,50,50); font-weight: bold;"
        elif eval_pnl < 0:
            css = "color: rgb(50,90,220); font-weight: bold;"
        else:
            css = "color: rgb(160,160,160);"
        if hasattr(self, "lbl_acct_eval"):
            self.lbl_acct_eval.setStyleSheet(css)
        if hasattr(self, "lbl_acct_pct"):
            self.lbl_acct_pct.setStyleSheet(css)

    def _calc_account_totals_from_trade_map(self):
        total_cost = 0
        total_eval = 0
        total_pnl = 0

        for code, t in self.trade_map.items():
            if t.pos_qty <= 0 or t.avg_price <= 0 or t.last <= 0:
                continue
            cost = int(t.avg_price * t.pos_qty)
            val = int(t.last * t.pos_qty)
            pnl = val - cost
            total_cost += cost
            total_eval += val
            total_pnl += pnl

        pct = ((total_eval / total_cost) - 1.0) * 100.0 if total_cost > 0 else 0.0
        return total_pnl, pct, total_eval

    def _apply_slippage(self, price: int, side: str) -> int:
        px = max(1, int(price))
        bps = self.slippage_bps_buy if side == "BUY" else self.slippage_bps_sell
        if bps <= 0:
            return px
        slip = max(1, int(round(px * (bps / 10000.0))))
        if side == "BUY":
            return px + slip
        return max(1, px - slip)

    def _current_day_pnl(self) -> int:
        unreal_pnl, _, _ = self._calc_account_totals_from_trade_map()
        return int(self.today_total_realized + unreal_pnl)

    def _daily_loss_limit_amount(self) -> int:
        if self.daily_budget <= 0 or self.daily_loss_limit_pct <= 0:
            return 0
        return int(self.daily_budget * self.daily_loss_limit_pct / 100.0)

    def _log_risk_once(self, key: str, message: str, throttle_sec: float = 20.0):
        now_ts = time.time()
        last_ts = self._risk_log_ts.get(key, 0.0)
        if now_ts - last_ts >= throttle_sec:
            self.log(message)
            self._risk_log_ts[key] = now_ts

    def _refresh_market_regime(self):
        scores: List[float] = []
        for code, t in self.trade_map.items():
            series, _ = self._series_3m(t)
            if len(series) < 65:
                continue
            ema20 = ema_series(series, 20)
            ema60 = ema_series(series, 60)
            if not ema20 or not ema60:
                continue
            mres = macd_series(series, self.macd_fast, self.macd_slow, self.macd_signal)
            hist = mres[2][-1] if mres is not None else 0.0
            score = 0.0
            if series[-1] > ema60[-1]:
                score += 1.0
            if ema60[-1] >= ema60[-2]:
                score += 1.0
            if hist > 0:
                score += 1.0
            scores.append(score / 3.0)

        prev_regime = self.market_regime
        if not scores:
            self.market_regime = "UNKNOWN"
            self.market_regime_breadth = 0.5
            return

        breadth = sum(1 for s in scores if s >= (2.0 / 3.0)) / len(scores)
        self.market_regime_breadth = float(breadth)
        if breadth >= 0.65:
            self.market_regime = "RISK_ON"
        elif breadth <= 0.35:
            self.market_regime = "RISK_OFF"
        else:
            self.market_regime = "NEUTRAL"

        if self.market_regime != prev_regime:
            self.log(f"[REGIME] {prev_regime} -> {self.market_regime} (breadth={breadth:.2f})")

    def _is_regime_blocked(self, signal_tag: str) -> bool:
        mode = str(getattr(self, "regime_filter_mode", "AUTO")).upper()
        regime = str(getattr(self, "market_regime", "UNKNOWN")).upper()

        if mode == "OFF" or regime == "UNKNOWN":
            return False
        if mode == "AUTO":
            if signal_tag == "BUY2_BREAKOUT":
                return regime != "RISK_ON"
            return regime == "RISK_OFF"
        if mode == "TREND_ONLY":
            return regime != "RISK_ON"
        if mode == "MEANREV_ONLY":
            return regime == "RISK_ON"
        return False

    def _enforce_daily_loss_limit(self):
        limit_amt = self._daily_loss_limit_amount()
        if limit_amt <= 0:
            return
        day_pnl = self._current_day_pnl()
        if day_pnl > -limit_amt:
            return

        if not self.daily_loss_hit:
            self.daily_loss_hit = True
            self.auto_trade_enabled = False
            self.log(
                f"[RISK] Daily loss limit hit: pnl={day_pnl:,} <= -{limit_amt:,}. "
                f"AutoTrade OFF + EXIT ALL"
            )

        for code, t in self.trade_map.items():
            if t.pos_qty > 0 and not t.pending_sell:
                self._sell_market(code, t.pos_qty, reason="DAILY_LOSS_LIMIT")

    def _update_account_pnl_ui(self, total_profit=None, total_buy=None, total_eval=None):

        if total_profit is not None:
            self.acct_total_profit = total_profit
        if total_buy is not None:
            self.acct_total_buy = total_buy
        if total_eval is not None:
            self.acct_total_eval = total_eval

        tp = getattr(self, "acct_total_profit", 0)
        tb = getattr(self, "acct_total_buy", 0)
        te = getattr(self, "acct_total_eval", 0)

        if tb > 0:
            pct = (tp / tb) * 100
        else:
            pct = 0.0

        # 계좌 손익 라벨 텍스트 갱신
        self.lbl_total_buy.setText(f"총매입: {tb:,}")
        self.lbl_total_eval.setText(f"총평가: {te:,}")
        self.lbl_total_profit.setText(f"총손익: {tp:,}")
        self.lbl_total_profit_rate.setText(f"수익률: {pct:.2f}%")

        color = "red" if tp > 0 else "blue" if tp < 0 else "black"

        self.lbl_total_profit.setStyleSheet(f"font-weight:bold; color:{color};")
        self.lbl_total_profit_rate.setStyleSheet(f"font-weight:bold; color:{color};")

    # =========================
    # UI Handlers (REAL / PRODUCTION)
    # =========================
    def _get_checked_candidate_codes(self):
        """Candidates 테이블의 체크된 코드 목록 반환"""
        tbl = getattr(self, "tbl_candidates", None)
        if tbl is None:
            self.log("[PICK] tbl_candidates missing")
            return []

        codes = []
        for r in range(tbl.rowCount()):
            it_chk = tbl.item(r, 0)  # Pick column
            it_code = tbl.item(r, 1)  # Code column
            if not it_chk or not it_code:
                continue
            if it_chk.checkState() == Qt.Checked:
                code = it_code.text().strip()
                if code:
                    codes.append(code)
        return codes

    def _on_pick_checked(self):
        # 기존 Trade List 코드 집합 수집
        existing_codes = set()
        for r in range(self.tbl_trade.rowCount()):
            it = self.tbl_trade.item(r, 0)
            if it:
                existing_codes.add(it.text().strip())

        """Candidates 체크 종목을 trade_map/Trade List로 편입"""
        codes = self._get_checked_candidate_codes()
        if not codes:
            self.log("[PICK] no checked candidates.")
            return

        pickn = int(self.spn_pickn.value()) if hasattr(self, "spn_pickn") else len(codes)
        codes = codes[:pickn]

        # 후보 종목 딕셔너리 참조
        cand_dict = getattr(self, "candidates", {})

        added = 0
        for code in codes:
            # 중복 편입 방지 체크
            if code in self.trade_map or code in existing_codes:
                if code in self.trade_map:
                    self.trade_map[code].manual_picked = True
                continue

            name = ""
            try:
                if code in cand_dict and "name" in cand_dict[code]:
                    name = str(cand_dict[code]["name"])
                else:
                    name = str(self.ocx.dynamicCall("GetMasterCodeName(QString)", code))
            except:
                name = ""

            # TradeItem 생성 시도
            try:
                t = TradeItem(code=code, name=name)
            except:
                # TradeItem 생성 실패 대비 폴백 객체
                class _T:
                    pass

                t = _T()
                t.code = code
                t.name = name

            # 필수 필드 기본값 보정
            for k, v in [
                ("last", 0), ("vol", 0), ("signal", ""),
                ("prev_o", 0), ("prev_c", 0), ("today_o", 0),
                ("pos_qty", 0), ("avg_price", 0.0),
                ("candles_3s", []), ("closes_3s", []),
                ("closes_3m", []), ("times_3m", []),
                ("closes_3m_calc", []), ("times_3m_calc", []),
                ("cur_3m_bucket", 0), ("cur_3m_close", 0.0),
            ]:
                if not hasattr(t, k):
                    setattr(t, k, v)

            self.trade_map[code] = t
            self.trade_map[code].manual_picked = True
            self.trade_map[code].auto_created_by_chejan = False
            self.trade_map[code].synced_from_balance = False
            added += 1

            # 헬퍼 함수가 있으면 행 추가 위임
            if hasattr(self, "_add_trade_row") and callable(getattr(self, "_add_trade_row")):
                self._add_trade_row(code)
            else:
                # Trade List 행을 직접 추가
                row = self.tbl_trade.rowCount()
                self.tbl_trade.insertRow(row)
                self.tbl_trade.setItem(row, 0, self._make_trade_code_item(code, checked=False))
                self.tbl_trade.setItem(row, 1, QTableWidgetItem(name))
                for c in range(2, self.tbl_trade.columnCount()):
                    self.tbl_trade.setItem(row, c, QTableWidgetItem(""))

        self.log(f"[PICK] moved to Trade List: added={added}, total_trade_map={len(self.trade_map)}")

    def _remove_trade_codes(self, codes: List[str], reason: str = "", block_holding: bool = True) -> Tuple[int, int]:
        removed_codes: List[str] = []
        skipped_holding = 0
        uniq_codes = [c for c in dict.fromkeys([str(x).strip() for x in codes]) if c]

        for code in uniq_codes:
            t = self.trade_map.get(code)
            if not t:
                continue

            pos_qty = int(getattr(t, "pos_qty", 0) or 0)
            if block_holding and pos_qty > 0:
                skipped_holding += 1
                continue

            self.trade_map.pop(code, None)
            removed_codes.append(code)

        if removed_codes:
            for code in removed_codes:
                try:
                    self.ocx.dynamicCall("SetRealRemove(QString, QString)", self.screen_real, code)
                except Exception:
                    pass

            if getattr(self, "display_code", None) in removed_codes:
                self.display_code = next(iter(self.trade_map.keys()), None)

            self._render_trade_list()
            self._update_account_summary_program()
            self._refresh_charts()

        if removed_codes:
            self.log(
                f"[TR.L REMOVE] reason={reason or '-'} removed={len(removed_codes)} "
                f"codes={','.join(removed_codes[:10])}"
            )
        return len(removed_codes), skipped_holding

    def _on_keep_holdings_only(self):
        if not self.trade_map:
            self.log("[TR.L CLEAN] trade_map empty")
            return
        targets = [code for code, t in self.trade_map.items() if int(getattr(t, "pos_qty", 0) or 0) <= 0]
        if not targets:
            self.log("[TR.L CLEAN] no non-holding codes to remove")
            return

        removed, skipped = self._remove_trade_codes(targets, reason="keep-holdings", block_holding=True)
        self.log(
            f"[TR.L CLEAN] keep holdings only -> removed={removed}, "
            f"skipped_holding={skipped}, total={len(self.trade_map)}"
        )

    def _on_remove_selected_trade(self):
        if self.tbl_trade.rowCount() <= 0:
            self.log("[TR.L REMOVE] trade list empty")
            return

        checked_codes = []
        for r in range(self.tbl_trade.rowCount()):
            it = self.tbl_trade.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                code = it.text().strip()
                if code:
                    checked_codes.append(code)

        if checked_codes:
            codes = checked_codes
        else:
            rows = set()
            for rg in self.tbl_trade.selectedRanges():
                for r in range(rg.topRow(), rg.bottomRow() + 1):
                    rows.add(r)

            if not rows:
                cur_row = self.tbl_trade.currentRow()
                if cur_row >= 0:
                    rows.add(cur_row)

            if not rows:
                self.log("[TR.L REMOVE] select row(s) or check code(s) first")
                return

            codes = []
            for r in sorted(rows):
                it = self.tbl_trade.item(r, 0)
                if it:
                    code = it.text().strip()
                    if code:
                        codes.append(code)

            if not codes:
                self.log("[TR.L REMOVE] no valid code selected")
                return

        holding_codes = []
        for code in codes:
            t = self.trade_map.get(code)
            if t and int(getattr(t, "pos_qty", 0) or 0) > 0:
                holding_codes.append(code)

        force_mode = False
        if holding_codes:
            msg = (
                "선택 종목 중 보유수량(>0) 종목이 있습니다.\n\n"
                "YES: 보유종목까지 강제 제거\n"
                "NO: 보유종목은 제외하고 제거\n"
                "CANCEL: 취소"
            )
            resp = QMessageBox.question(
                self,
                "보유 종목 제거 확인",
                msg,
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.No
            )
            if resp == QMessageBox.Cancel:
                self.log("[TR.L REMOVE] canceled by user")
                return
            force_mode = (resp == QMessageBox.Yes)

        removed, skipped = self._remove_trade_codes(
            codes,
            reason="manual-remove-force" if force_mode else "manual-remove",
            block_holding=(not force_mode)
        )
        self.log(
            f"[TR.L REMOVE] selected={len(codes)} removed={removed} "
            f"skipped_holding={skipped} force={force_mode}"
        )

    def _ensure_apply_tr_timer(self):
        """TR 큐를 안전하게 순차 처리하는 타이머 핸들러"""
        if not hasattr(self, "apply_tr_q") or self.apply_tr_q is None:
            self.apply_tr_q = []

        if not hasattr(self, "apply_tr_timer") or self.apply_tr_timer is None:
            self.apply_tr_timer = QTimer(self)
            self.apply_tr_timer.timeout.connect(self._drain_apply_tr_q)

        if not self.apply_tr_timer.isActive():
            self.apply_tr_timer.start(350)  # TR 적용 큐 타이머 시작(350ms)

    def _on_setrealreg_and_tr_picked(self):
        """
        Pick된 trade_map 종목에 대해:
        1) SetRealReg(현재가/거래량/체결시간)
        2) 기본정보(opt10001) + 3분봉(opt10080) TR 순차 요청
        """
        if not self.connected:
            self.log("[SETREAL+TR] not connected.")
            return
        if not self.trade_map:
            self.log("[SETREAL+TR] trade_map empty. Pick first.")
            return

        # 전용 RealReg 함수 우선 사용
        if hasattr(self, "_set_realreg_for_trade_map") and callable(getattr(self, "_set_realreg_for_trade_map")):
            self._set_realreg_for_trade_map()
        else:
            # 실시간 등록 코드/FID 구성
            codes = ";".join(self.trade_map.keys())
            fid_list = "10;15;20"
            self.ocx.dynamicCall("SetRealReg(QString, QString, QString, QString)", self.screen_real, codes, fid_list,
                                 "0")
            self.log(f"[RealReg] synced codes={len(self.trade_map)}")

        # 기존 TR 큐 초기화
        self.apply_tr_q.clear()

        for code in list(self.trade_map.keys()):
            # 기본정보 TR 작업 큐잉
            if hasattr(self, "_req_basic_info") and callable(getattr(self, "_req_basic_info")):
                self.apply_tr_q.append((self._req_basic_info, (code,)))

            # 3분봉 TR 작업 큐잉
            if hasattr(self, "_req_3m_history") and callable(getattr(self, "_req_3m_history")):
                self.apply_tr_q.append((self._req_3m_history, (code, 400)))
            # 일봉 TR 작업 큐잉
            if hasattr(self, "_req_daily_history"):
                self.apply_tr_q.append((self._req_daily_history, (code, 3)))

        self.log(f"[SETREAL+TR] queued TR jobs={len(self.apply_tr_q)}")
        self._ensure_apply_tr_timer()

    def _req_daily_history(self, code: str, cnt: int = 3):
        rqname = f"TR_DAILY_PREV_{code}"
        self.tr_wait[rqname] = {"type": "daily_prev", "code": code}

        self.ocx.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "기준일자", "")
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")

        self.ocx.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname,
            "opt10081",
            0,
            self.screen_tr
        )

    def _on_toggle_auto_trade(self):
        """Auto Trade ON/OFF 토글"""
        cur = bool(getattr(self, "auto_trade_enabled", False))
        nxt = not cur
        if nxt and self.daily_loss_hit:
            self.log("[AUTO-BLOCK] daily loss limit hit 상태라 Auto Trade를 켤 수 없습니다.")
            nxt = False
        self.auto_trade_enabled = nxt

        if hasattr(self, "btn_auto_trade"):
            self.btn_auto_trade.setText("Auto Trade ON" if nxt else "Auto Trade OFF")

        self.log(f"[AUTO] auto_trade_enabled={self.auto_trade_enabled}")

    def _on_toggle_arm_live(self):
        """ARM LIVE(실주문) ON/OFF 토글"""
        cur = bool(getattr(self, "arm_live", False))
        nxt = not cur
        self.arm_live = nxt

        if hasattr(self, "btn_arm"):
            self.btn_arm.setText("ARM LIVE (실주문) ON" if nxt else "ARM LIVE (실주문) OFF")

        self.log(f"[ARM] ARM LIVE set to {self.arm_live}")

    # -------------------------
    # Logging
    # -------------------------
    def log(self, msg: str):
        # 로그 버퍼 안전 초기화
        if not hasattr(self, "logs") or self.logs is None:
            self.logs = []

        line = f"[{now_str()}] {msg}"
        self.logs.append(line)

        # 로그창 출력(실패 시 무시)
        if hasattr(self, "txt_logs") and self.txt_logs is not None:
            try:
                self.txt_logs.append(line)
            except Exception:
                pass

    def _update_account_pnl_ui(self, total_profit=None, total_buy=None, total_eval=None):

        if total_buy is None:
            total_buy = 0
        if total_eval is None:
            total_eval = 0
        if total_profit is None:
            total_profit = total_eval - total_buy

        rate = 0.0
        if total_buy > 0:
            rate = (total_profit / total_buy) * 100

        # 계좌 손익 라벨 텍스트 갱신
        self.lbl_total_buy.setText(f"총매입: {total_buy:,}")
        self.lbl_total_eval.setText(f"총평가: {total_eval:,}")
        self.lbl_total_profit.setText(f"총손익: {total_profit:,}")
        self.lbl_total_profit_rate.setText(f"수익률: {rate:.2f}%")

        # 손익 부호에 따라 색상 결정
        color = "red" if total_profit > 0 else "blue" if total_profit < 0 else "black"

        self.lbl_total_profit.setStyleSheet(f"font-weight:bold; color:{color};")
        self.lbl_total_profit_rate.setStyleSheet(f"font-weight:bold; color:{color};")

    # -------------------------
    # Config
    # -------------------------
    def _apply_config(self):
        self.daily_budget = to_int(self.ed_budget.text(), 0)
        self.split_1 = int(self.ed_split1.value())
        self.split_2 = int(self.ed_split2.value())
        self.macd_fast = int(self.ed_macd_fast.value())
        self.macd_slow = int(self.ed_macd_slow.value())
        self.macd_signal = int(self.ed_macd_sig.value())
        self.rsi_buy_level = int(self.ed_rsi_buy.text())
        self.daily_loss_limit_pct = max(0.0, to_float(self.ed_daily_loss_pct.text(), 2.0))
        self.slippage_bps_buy = max(0, to_int(self.ed_slippage_buy_bps.text(), 8))
        self.slippage_bps_sell = max(0, to_int(self.ed_slippage_sell_bps.text(), 8))
        self.regime_filter_mode = str(self.cmb_regime_filter.currentText()).strip().upper()
        self.log(f"[CONFIG] RSI Buy Level set to {self.rsi_buy_level}")
        self.log(
            f"[CONFIG] risk: loss_limit={self.daily_loss_limit_pct:.2f}% "
            f"regime={self.regime_filter_mode} "
            f"slippage_buy={self.slippage_bps_buy}bps slippage_sell={self.slippage_bps_sell}bps"
        )

        actual_n = len(self.trade_map) if hasattr(self, "trade_map") else 0
        if actual_n <= 0:
            per_stock = 0
        else:
            per_stock = self.daily_budget / actual_n

        self.lbl_budget_hint.setText(
            f"Budget Hint: PerStock={int(per_stock):,} "
            f"(1st={int(per_stock * self.split_1 / 100):,}, 2nd={int(per_stock * self.split_2 / 100):,}) "
            f"| today_spent={self.today_spent:,} "
            f"| loss_limit={int(self.daily_budget * self.daily_loss_limit_pct / 100):,}"
        )
        # 차트 RSI 기준선 동기화
        if hasattr(self, "rsi_buy_line"):
            self.rsi_buy_line.setValue(self.rsi_buy_level)

        if hasattr(self, "rsi_sell_line"):
            self.rsi_sell_line.setValue(self.rsi_sell_level)
        self.log("Config applied.")

    def _per_stock_budget(self) -> int:
        """
        종목당 예산은 실제 Trade List 종목 수 기준으로 계산.
        spn_topn은 후보 수 용도로 사용.
        """
        if not hasattr(self, "trade_map"):
            return 0

        actual_n = len(self.trade_map)

        if actual_n <= 0:
            return 0

        return int(self.daily_budget / actual_n)

    # -------------------------
    # Daily flags for stoploss
    # -------------------------
    def _today_yyyymmdd(self) -> str:
        return datetime.datetime.now().strftime("%Y%m%d")


    def _reset_daily_flags_if_needed(self, t: TradeItem):
        today = self._today_yyyymmdd()
        if t.stop_day != today:
            t.stop_day = today
            t.stop1_done = False
            t.eod_checked = False  # 새 거래일 진입 시 EOD 체크 초기화

    # -------------------------
    # Login / Accounts
    # -------------------------
    def _login(self):
        self.log("Connecting/Login...")
        self.ocx.dynamicCall("CommConnect()")

    def _on_click_login(self):
        try:
            if not hasattr(self, "ocx") or self.ocx is None:
                self.log("[LOGIN] ocx is None (KHOpenAPI control not created)")
                return

            state = int(self.ocx.dynamicCall("GetConnectState()"))
            self.log(f"[LOGIN] GetConnectState={state}")

            if state == 1:
                self.log("[LOGIN] already connected")
                self._set_status_connected_ui(True)
                return

            ret = self.ocx.dynamicCall("CommConnect()")
            self.log(f"[LOGIN] CommConnect() called ret={ret}")
        except Exception as e:
            self.log(f"[LOGIN] error: {e}")

    def _on_event_connect(self, err_code):
        if int(err_code) == 0:
            self.connected = True
            self.lbl_status.setText("Status: CONNECTED (Login OK)")
            self.log("Login OK.")
            self._load_accounts()
            QTimer.singleShot(1000, self._load_conditions)
            self.log("[COND] scheduled GetConditionLoad after login (1s)")
            QTimer.singleShot(2000, self._request_balance_opw00018)
        else:
            self.connected = False
            self.lbl_status.setText(f"Status: LOGIN FAIL ({err_code})")
            self.log(f"Login failed: {err_code}")

    def check_market_open(self):
        now_dt = datetime.datetime.now()
        now = now_dt.time()

        # =========================
        # 장 상태/일일 플래그 관리
        # =========================
        if now >= self.market_open_time:
            if self.is_warmup:
                self.is_warmup = False
                self.auto_trade_enabled = True
                self.log("[WARMUP] MARKET OPEN -> AutoTrade ENABLED")

        # =========================
        # 장 상태/일일 플래그 관리
        # =========================
        if now >= datetime.time(15, 30) and not self._summary_done:
            self._summary_done = True
            self.log("[SUMMARY] 15:30 auto daily observer summary")
            self._print_daily_summary()

        # =========================
        # 장 상태/일일 플래그 관리
        # =========================
        if getattr(self, "_summary_date", None) != now_dt.date():
            self._summary_date = now_dt.date()
            self._summary_done = False
            self.daily_loss_hit = False
            self._risk_log_ts.clear()

    def _ensure_tradeitem_from_balance(self, code: str, name: str, qty: int, avg: float, cur: int):
        """
        opw00018 보유종목을 trade_map 및 UI와 동기화.
        - trade_map에 없으면 생성
        - pos_qty/avg_price/last 갱신
        - Trade List UI 반영
        """
        code = (code or "").strip().replace("A", "")
        if not code:
            return

        qty = int(qty)
        if qty <= 0:
            t0 = self.trade_map.get(code)
            if t0 and bool(getattr(t0, "synced_from_balance", False)) and not bool(getattr(t0, "manual_picked", False)):
                removed, _ = self._remove_trade_codes([code], reason="balance-qty0-auto-exclude", block_holding=True)
                if removed > 0:
                    self.log(f"[BAL->MAP] auto-excluded qty=0 code={code}")
            return

        if code not in self.trade_map:
            self.trade_map[code] = TradeItem(code=code, name=str(name).strip())
            self.trade_map[code].synced_from_balance = True
            self.trade_map[code].manual_picked = False
            self.log(f"[BAL->MAP] added holding -> trade_map: {code} {name} qty={qty} avg={avg:.2f} cur={cur}")

        # 잔고 종목이 테이블에 없으면 다시 렌더링
        if code not in [
            self.tbl_trade.item(r, 0).text()
            for r in range(self.tbl_trade.rowCount())
            if self.tbl_trade.item(r, 0)
        ]:
            self._render_trade_list()

        t = self.trade_map[code]

        # 잔고 기준 포지션 값 동기화
        t.name = str(name).strip() or t.name
        t.pos_qty = int(qty)
        t.avg_price = float(avg)
        if int(cur) > 0:
            t.last = int(cur)

        t.signal = "HOLD" if t.pos_qty > 0 else "WAIT"
        t.synced_from_balance = True

        # 동기화 결과를 Trade List에 반영
        self._update_trade_row(
            code,
            pos_qty=t.pos_qty,
            avg_price=t.avg_price,
            last=t.last,
            signal=t.signal
        )

        self.log(f"[DEBUG] updating row for {code}")

    def _load_accounts(self):
        accs = self.ocx.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        acc_list = [a for a in str(accs).strip().split(";") if a.strip()]
        user_id = self.ocx.dynamicCall("GetLoginInfo(QString)", "USER_ID")
        self.cmb_account.clear()
        self.cmb_account.addItems(acc_list)
        self.log(f"Accounts loaded: {acc_list}")
        self.log(f"접속아이디:{user_id}")

    # -------------------------
    # Conditions
    # -------------------------
    def _load_conditions(self):
        # OpenAPI 연결 상태 재확인
        try:
            state = int(self.ocx.dynamicCall("GetConnectState()"))
        except Exception:
            state = -1

        if not getattr(self, "connected", False) or state != 1:
            self.log(
                f"[COND] Not connected. self.connected={getattr(self, 'connected', False)} GetConnectState={state}")
            return

        self.log("[COND] Loading conditions... (calling GetConditionLoad)")
        try:
            ret = self.ocx.dynamicCall("GetConditionLoad()")
            self.log(f"[COND] GetConditionLoad() ret={ret} (wait OnReceiveConditionVer)")
        except Exception as e:
            self.log(f"[COND] GetConditionLoad error: {e}")

    def _on_receive_condition_ver(self, ret, msg):
        self.log(f"ConditionVer ret={ret} msg={msg}")
        raw = self.ocx.dynamicCall("GetConditionNameList()")
        raw_s = str(raw).strip() if raw is not None else ""
        self.log(f"[COND] raw='{raw_s[:120]}'")  # 조건식 원문 일부를 디버그 로그로 기록
        if not raw_s:
            self.log("[COND] GetConditionNameList() is EMPTY. (HTS에서 조건검색식을 서버에 저장했는지 확인 필요)")

        self.conditions.clear()
        self.cmb_cond.clear()
        if raw:
            for chunk in str(raw).split(";"):
                if "^" in chunk:
                    idx, name = chunk.split("^", 1)
                    idx = int(idx)
                    name = name.strip()
                    if name:
                        self.conditions.append((idx, name))
                        self.cmb_cond.addItem(name, idx)
        self.log(f"Loaded conditions: {len(self.conditions)}")

    def _run_condition_to_candidates(self):
        self.log("[COND] _run_condition_to_candidates CALLED")
        if not self.connected:
            self.log("Not connected.")
            return
        if self.cmb_cond.count() == 0:
            self.log("No conditions.")
            return

        cond_name = self.cmb_cond.currentText()
        cond_idx = int(self.cmb_cond.currentData())
        self.candidates.clear()
        self._clear_table(self.tbl_candidates)

        screen = "7000"
        self.log(f"Run condition: {cond_name} ({cond_idx})")
        self.ocx.dynamicCall("SendCondition(QString, QString, int, int)", screen, cond_name, cond_idx, 0)

    def _on_receive_tr_condition(self, sScrNo, strCodeList, strConditionName, nIndex, nNext):
        code_list = [c for c in str(strCodeList).split(";") if c.strip()]
        self.log(f"Condition result: {strConditionName} -> {len(code_list)} codes")

        for code in code_list:
            name = self.ocx.dynamicCall("GetMasterCodeName(QString)", code)
            self.candidates[code] = {"code": code, "name": str(name), "price": 0, "vol": 0, "value": 0, "note": ""}

        self._render_candidates_topn()

        # TopN 기준 조회 범위 설정
        topn = int(self.spn_topn.value())
        # 기본정보 TR 큐와 타이머 초기화
        self.basic_q.clear()
        # 기본정보 TR 큐와 타이머 초기화
        self.basic_q = code_list[:topn]
        # 기본정보 TR 큐 처리 시작
        QTimer.singleShot(0, self._drain_basic_q)
    # -------------------------
    # Candidates: Basic info TR (opt10001)
    # -------------------------
    def _drain_basic_q(self):
        if not self.connected:
            return
        if not self.basic_q:
            return
        code = self.basic_q.pop(0)
        self._req_basic_info(code)

    def _req_basic_info(self, code: str):
        rqname = f"TR_BASIC_{code}"
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.tr_wait[rqname] = {"type": "basic", "code": code}
        self.ocx.dynamicCall("CommRqData(QString, QString, int, QString)", rqname, "opt10001", 0, self.screen_tr)

    def _render_candidates_topn(self):
        topn = int(self.spn_topn.value())
        codes = list(self.candidates.keys())[:topn]

        self.tbl_candidates.setRowCount(0)
        for code in codes:
            d = self.candidates[code]
            row = self.tbl_candidates.rowCount()
            self.tbl_candidates.insertRow(row)

            chk = QTableWidgetItem("")
            chk.setCheckState(Qt.Unchecked)
            self.tbl_candidates.setItem(row, 0, chk)

            self.tbl_candidates.setItem(row, 1, QTableWidgetItem(code))
            self.tbl_candidates.setItem(row, 2, QTableWidgetItem(d["name"]))
            self.tbl_candidates.setItem(row, 3, QTableWidgetItem(str(d["price"])))
            self.tbl_candidates.setItem(row, 4, QTableWidgetItem(str(d["vol"])))
            self.tbl_candidates.setItem(row, 5, QTableWidgetItem(str(d["value"])))
            self.tbl_candidates.setItem(row, 6, QTableWidgetItem(str(d["note"])))

    # -------------------------
    # Pick -> Trade List
    # -------------------------
    def _pick_checked_to_trade_list(self):
        pickn = int(self.spn_pickn.value())
        picked = []
        for r in range(self.tbl_candidates.rowCount()):
            it = self.tbl_candidates.item(r, 0)
            if it and it.checkState() == Qt.Checked:
                code = self.tbl_candidates.item(r, 1).text().strip()
                picked.append(code)
        picked = picked[:pickn]

        if not picked:
            self.log("No picked candidates.")
            return

        for code in picked:
            if code not in self.trade_map:
                name = self.ocx.dynamicCall("GetMasterCodeName(QString)", code)
                self.trade_map[code] = TradeItem(code=code, name=str(name))
            self.trade_map[code].manual_picked = True
            self.trade_map[code].auto_created_by_chejan = False

        self._render_trade_list()
        self.log(f"Picked -> Trade List: {picked}")

        if self.display_code is None and self.trade_map:
            self.display_code = next(iter(self.trade_map.keys()))
            self._refresh_charts()

    def _render_trade_list(self):
        checked_codes = set()
        for r in range(self.tbl_trade.rowCount()):
            it0 = self.tbl_trade.item(r, 0)
            if it0 and it0.checkState() == Qt.Checked:
                code0 = it0.text().strip()
                if code0:
                    checked_codes.add(code0)

        self.tbl_trade.setRowCount(0)
        for code, t in self.trade_map.items():
            row = self.tbl_trade.rowCount()
            self.tbl_trade.insertRow(row)

            self.tbl_trade.setItem(row, 0, self._make_trade_code_item(t.code, checked=(t.code in checked_codes)))
            self.tbl_trade.setItem(row, 1, QTableWidgetItem(t.name))
            self.tbl_trade.setItem(row, 2, QTableWidgetItem(str(t.last)))
            self.tbl_trade.setItem(row, 3, QTableWidgetItem(str(t.vol)))
            self.tbl_trade.setItem(row, 4, QTableWidgetItem(t.signal))
            self.tbl_trade.setItem(row, 5, QTableWidgetItem(str(t.prev_o)))
            self.tbl_trade.setItem(row, 6, QTableWidgetItem(str(t.prev_c)))
            self.tbl_trade.setItem(row, 7, QTableWidgetItem(str(t.today_o)))
            self.tbl_trade.setItem(row, 8, QTableWidgetItem(str(t.pos_qty)))
            self.tbl_trade.setItem(row, 9, QTableWidgetItem(f"{t.avg_price:.2f}"))

            # 평가손익 계산 후 표시
            eval_pnl, pnl_pct = self._calc_eval_pnl(t)
            self.tbl_trade.setItem(row, 10, QTableWidgetItem(f"{eval_pnl:,}"))
            self.tbl_trade.setItem(row, 11, QTableWidgetItem(f"{pnl_pct:.2f}"))
            self._set_trade_pnl_color(row, eval_pnl, pnl_pct)

            # 지표 컬럼(MACD/RSI) 표시
            self.tbl_trade.setItem(row, 12, QTableWidgetItem(str(len(t.candles_3s))))
            self.tbl_trade.setItem(row, 13, QTableWidgetItem(
                f"{t.macd_3m:.2f}" if getattr(t, "macd_3m", None) is not None else "-"))
            self.tbl_trade.setItem(row, 14, QTableWidgetItem(
                f"{t.rsi_3m:.2f}" if getattr(t, "rsi_3m", None) is not None else "-"))

    def _set_cell_safe(self, row: int, col: int, text: str):
        it = self.tbl_trade.item(row, col)
        if it is None:
            it = QTableWidgetItem("")
            self.tbl_trade.setItem(row, col, it)
        it.setText(str(text))

    # =========================
    # 테이블 셀 안전 갱신
    # 테이블 셀 안전 갱신
    # =========================
    def _update_trade_row(self, code: str, **kwargs):
        t = self.trade_map.get(code)
        if not t:
            return

        for k, v in kwargs.items():
            setattr(t, k, v)

        for r in range(self.tbl_trade.rowCount()):
            code_item = self.tbl_trade.item(r, 0)
            if not code_item:
                continue
            if code_item.text().strip() != code:
                continue

            # 행 기본 시세/포지션 값 갱신
            self._set_cell_safe(r, 2, t.last)
            self._set_cell_safe(r, 3, t.vol)
            self._set_cell_safe(r, 4, t.signal)
            self._set_cell_safe(r, 5, t.prev_o)
            self._set_cell_safe(r, 6, t.prev_c)
            self._set_cell_safe(r, 7, t.today_o)
            self._set_cell_safe(r, 8, t.pos_qty)
            self._set_cell_safe(r, 9, f"{t.avg_price:.2f}")

            # 평가손익 계산 후 표시
            eval_pnl, pnl_pct = self._calc_eval_pnl(t)
            self._set_cell_safe(r, 10, f"{eval_pnl:,}")
            self._set_cell_safe(r, 11, f"{pnl_pct:.2f}")
            self._set_trade_pnl_color(r, eval_pnl, pnl_pct)  # 손익 색상 즉시 반영
            # 계좌 요약 라벨 재계산
            self._update_account_pnl_ui()
            # 손익률 기준으로 테이블 재정렬
            self._sort_trade_table(11)   # 11=PnL(%)

            # 지표 셀(MACD/RSI) 동기화

            if hasattr(t, "macd_3m") and t.macd_3m is not None:
                self._set_cell_safe(r, 12, f"{t.macd_3m:.2f}")
            else:
                self._set_cell_safe(r, 12, "-")

            if hasattr(t, "rsi_3m") and t.rsi_3m is not None:
                self._set_cell_safe(r, 13, f"{t.rsi_3m:.2f}")
            else:
                self._set_cell_safe(r, 13, "-")

            return

    def _set_macd_cell(self, code: str, text: str):
        for r in range(self.tbl_trade.rowCount()):
            it_code = self.tbl_trade.item(r, 0)
            if not it_code:
                continue
            if it_code.text().strip() == code:
                col = 13  # MACD 컬럼 인덱스
                it = self.tbl_trade.item(r, col)
                if it is None:
                    it = QTableWidgetItem("")
                    self.tbl_trade.setItem(r, col, it)
                it.setText(str(text))
                return

    def _set_rsi_cell(self, code: str, text: str):
        for r in range(self.tbl_trade.rowCount()):
            it_code = self.tbl_trade.item(r, 0)
            if not it_code:
                continue
            if it_code.text().strip() == code:
                col = 14  # RSI 컬럼 인덱스
                it = self.tbl_trade.item(r, col)
                if it is None:
                    it = QTableWidgetItem("")
                    self.tbl_trade.setItem(r, col, it)
                it.setText(str(text))
                return



    def _sort_trade_table(self, key_col: int):
        """
        key_col: 10(EvalPnL) or 11(PnL%)
        숫자 기반 정렬(문자열 정렬 방지)
        """
        # 정렬용 (코드,값) 목록 생성
        rows = []
        for r in range(self.tbl_trade.rowCount()):
            code_it = self.tbl_trade.item(r, 0)
            if not code_it:
                continue
            code = code_it.text().strip()
            it = self.tbl_trade.item(r, key_col)
            txt = it.text().replace(",", "").strip() if it else "0"
            try:
                val = float(txt)
            except:
                val = 0.0
            rows.append((code, val))

        # 손익 기준 내림차순 정렬
        rows.sort(key=lambda x: x[1], reverse=True)

        # 현재 테이블 데이터 스냅샷(코드 체크상태 포함)
        snap = {}
        for r in range(self.tbl_trade.rowCount()):
            vals = []
            checked = False
            for c in range(self.tbl_trade.columnCount()):
                it = self.tbl_trade.item(r, c)
                if c == 0 and it is not None:
                    checked = (it.checkState() == Qt.Checked)
                vals.append("" if it is None else it.text())
            if vals and vals[0].strip():
                snap[vals[0].strip()] = (vals, checked)

        for new_r, (code, _) in enumerate(rows):
            packed = snap.get(code)
            if not packed:
                continue
            vals, checked = packed
            # code 컬럼은 체크박스 유지
            self.tbl_trade.setItem(new_r, 0, self._make_trade_code_item(vals[0], checked=checked))
            for c, txt in enumerate(vals[1:], start=1):
                self._set_cell_safe(new_r, c, txt)

        # 정렬 후 손익 색상 재적용
        for r in range(self.tbl_trade.rowCount()):
            code_it = self.tbl_trade.item(r, 0)
            if not code_it:
                continue
            code = code_it.text().strip()
            t = self.trade_map.get(code)
            if not t:
                continue
            eval_pnl, pnl_pct = self._calc_eval_pnl(t)
            self._set_trade_pnl_color(r, eval_pnl, pnl_pct)

    def _on_trade_list_clicked(self, row: int, col: int):
        code_item = self.tbl_trade.item(row, 0)
        if not code_item:
            return
        code = code_item.text().strip()
        if code and code in self.trade_map:
            self.display_code = code
            self.log(f"[CHART] display_code -> {code}")
            self._refresh_charts()

        else:
            self.log(f"[CHART] invalid code clicked: {code}")  # 유효하지 않은 코드 클릭 로그
    # -------------------------
    # Apply Picked: RealReg + Prev(TR) + 3m bootstrap(TR)
    # -------------------------
    def _apply_picked(self):
        if not self.connected:
            self.log("Not connected.")
            return
        if not self.trade_map:
            self.log("Trade list empty.")
            return
        codes = ";".join(self.trade_map.keys())

        # 실시간 수신 FID(현재가/거래량/체결시간)
        fid_list = "10;15;20"
        self.log(f"SetRealReg codes={codes}")
        self.ocx.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            self.screen_real, codes, fid_list, "0"
        )
        self.daily_sum.apply_picked_count += 1
        self.daily_sum.realreg_codes_last = codes
        self.daily_sum.realreg_code_count = len(self.trade_map.keys())

        # Apply Picked TR 큐 재구성
        self.apply_tr_q = []
        for code in list(self.trade_map.keys()):
            self.apply_tr_q.append((self._req_prev_ohlc, (code,)))  # opt10001
            self.apply_tr_q.append((self._req_prev_day_ohlc_by_daily, (code,)))  # opt10081
            self.apply_tr_q.append((self._req_3m_history, (code, 400)))  # opt10080

        # TR 큐 순차 실행 타이머 시작
        self.apply_tr_timer.start(350)
        self._request_3m_warmup(code)
        self.log("Apply Picked: queued RealReg + opt10001 + opt10081 + opt10080")

    def _req_prev_ohlc(self, code: str):
        """
        PrevO/PrevC(전일 시가/종가) 조회용 TR.
        -> opt10081(일봉차트조회) 사용
        """
        code = code.strip()
        if not code:
            return

        rqname = f"TR_PREV_{code}"  # 전일값 조회 요청명 생성

        self.ocx.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.tr_wait[rqname] = {"type": "prev_opt10001", "code": code}

        self.ocx.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            rqname,
            "opt10001",
            0,
            self.screen_tr
        )

        self.log(f"[TR] opt10001 requested for PrevOHLC: {code}")

    def _req_prev_day_ohlc_by_daily(self, code: str):
        """
        전일(일봉) 시가/종가를 정확히 가져오기 위해 opt10081 사용
        """
        code = code.strip()
        if not code:
            return

        rqname = f"TR_DAILY_PREV_{code}"
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "기준일자", datetime.datetime.now().strftime("%Y%m%d"))
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")

        self.tr_wait[rqname] = {"type": "daily_prev", "code": code}

        # 전일 일봉 OHLC TR 요청 전송
        self.ocx.dynamicCall("CommRqData(QString, QString, int, QString)", rqname, "opt10081", 0, self.screen_tr)
        self.log(f"[TR] opt10081 requested for prev-day OHLC: {code}")

    def _request_3m_warmup(self, code):
        self.log(f"[WARMUP REQUEST] {code}")

        self.ocx.dynamicCall(
            "SetInputValue(QString, QString)",
            "종목코드", code
        )
        self.ocx.dynamicCall(
            "SetInputValue(QString, QString)",
            "틱범위", "3"
        )
        self.ocx.dynamicCall(
            "SetInputValue(QString, QString)",
            "수정주가구분", "1"
        )

        self.ocx.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            f"{code}_3m_warmup",
            "opt10080",
            0,
            "0101"
        )
        # 중복 워밍업 요청 방지
        if self.warmup_ready.get(code, False):
            self.log(f"[WARMUP] already ready: {code}")
            return
    # -------------------------
    # 3분봉 워밍업 요청 관리
    # -------------------------
    def _req_3m_history(self, code: str, cnt: int = 400):
        self.log(f"[REQ 3m] {code} cnt={cnt}")  # 3분봉 히스토리 요청 로그
        rqname = f"TR_3M_{code}"
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "틱범위", "3")  # 3분봉
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "수정주가구분", "1")
        self.tr_wait[rqname] = {"type": "hist_3m", "code": code, "cnt": cnt}
        self.ocx.dynamicCall("CommRqData(QString, QString, int, QString)", rqname, "opt10080", 0, self.screen_tr)

    def _update_account_summary_program(self):
        """
        프로그램 기준 당일 손익 요약 계산
        """

        # 당일 총 매수 집행금액
        total_buy = self.today_total_spent

        # 보유 평가금액/미실현손익 초기화
        total_eval = 0
        unreal_pnl = 0

        for code, t in self.trade_map.items():

            qty = getattr(t, "pos_qty", 0)
            avg = getattr(t, "avg_price", 0)
            last = getattr(t, "last_price", 0)

            if qty > 0 and last > 0:
                eval_amt = qty * last
                total_eval += eval_amt
                unreal_pnl += (last - avg) * qty

        # 당일 실현손익 반영
        realized = self.today_total_realized

        # 총손익 = 실현 + 미실현
        total_profit = realized + unreal_pnl

        # 수익률 계산
        rate = 0.0
        if total_buy > 0:
            rate = (total_profit / total_buy) * 100

        # 요약 라벨 안전 갱신
        if hasattr(self, "lbl_total_buy"):
            self.lbl_total_buy.setText(f"{total_buy:,}")
        if hasattr(self, "lbl_total_eval"):
            self.lbl_total_eval.setText(f"{total_eval:,}")
        if hasattr(self, "lbl_total_profit"):
            self.lbl_total_profit.setText(f"{total_profit:,}")
        if hasattr(self, "lbl_total_rate"):
            self.lbl_total_rate.setText(f"{rate:.2f}%")
        print(
            f"[ACCOUNT PROGRAM] "
            f"매입={total_buy:,} "
            f"평가={total_eval:,} "
            f"실현={realized:,} "
            f"미실현={unreal_pnl:,} "
            f"총손익:{total_profit:,}"
        )

    def _save_today_summary(self):

        today_str = datetime.now().strftime("%Y-%m-%d")

        data = {
            "date": today_str,
            "spent": self.today_total_spent,
            "realized": self.today_total_realized
        }

        with open("today_summary.json", "w") as f:
            json.dump(data, f)

        print("[SUMMARY SAVE] 저장 완료")

    def _load_today_summary(self):

        if not os.path.exists("today_summary.json"):
            print("[SUMMARY LOAD] 파일 없음")
            return

        with open("today_summary.json", "r") as f:
            data = json.load(f)

        today_str = datetime.now().strftime("%Y-%m-%d")

        if data.get("date") == today_str:
            self.today_total_spent = data.get("spent", 0)
            self.today_total_realized = data.get("realized", 0)
            print("[SUMMARY LOAD] 당일 실적 복원 완료")
        else:
            print("[SUMMARY LOAD] 날짜 변경으로 실적 초기화")

    def _request_balance_opw00018(self):
        acc = self.cmb_account.currentText().strip()
        if not acc:
            self.log("[BAL] account empty")
            return
        if self.ocx.GetConnectState() != 1:
            self.log("[BAL] not connected")
            return

        self.ocx.dynamicCall("SetInputValue(QString, QString)", "계좌번호", acc)
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "비밀번호", "")
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "비밀번호입력매체구분", "00")
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "조회구분", "2")

        self.log(f"[BAL] Request opw00018 acc={acc}")

        self.ocx.dynamicCall(
            "CommRqData(QString, QString, int, QString)",
            "RQ_BAL_OPW00018", "opw00018", 0, self.screen_tr
        )

    def _handle_balance_opw00018(self, trcode, recordName):
        """
        opw00018 응답 처리:
        - 계좌 합계(총매입/총평가/총손익) 파싱
        - 보유종목을 trade_map + UI와 동기화
        """
        try:
            total_buy = abs(to_int(self.ocx.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                trcode, recordName, 0, "총매입금액"
            )))
            total_eval = abs(to_int(self.ocx.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                trcode, recordName, 0, "총평가금액"
            )))
            total_profit = to_int(self.ocx.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                trcode, recordName, 0, "총평가손익금액"
            ))

            if total_buy <= 0:
                total_buy = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, recordName, 0, "매입금액"
                )))
            if total_eval <= 0:
                total_eval = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, recordName, 0, "평가금액"
                )))
            if total_profit == 0:
                total_profit = to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, recordName, 0, "평가손익"
                ))

            data_cnt = to_int(self.ocx.dynamicCall("GetRepeatCnt(QString, QString)", trcode, recordName))
            hold_buy = 0
            hold_eval = 0
            synced = 0
            seen_holding_codes = set()
            for i in range(data_cnt):
                code = clean_code(str(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, recordName, i, "종목번호"
                )).strip())
                name = str(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, recordName, i, "종목명"
                )).strip()
                qty = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, recordName, i, "보유수량"
                )))
                avg = abs(to_float(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, recordName, i, "평균단가"
                )))
                if avg <= 0:
                    avg = abs(to_float(self.ocx.dynamicCall(
                        "GetCommData(QString, QString, int, QString)",
                        trcode, recordName, i, "매입가"
                    )))
                cur = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, recordName, i, "현재가"
                )))

                if code:
                    self._ensure_tradeitem_from_balance(code, name, qty, avg, cur)
                    if qty > 0:
                        seen_holding_codes.add(code)
                        synced += 1

                if qty > 0 and avg > 0:
                    hold_buy += int(qty * avg)
                if qty > 0:
                    mark = int(cur) if cur > 0 else int(avg)
                    hold_eval += int(qty * mark)

            stale_codes = []
            for c, t in list(self.trade_map.items()):
                if not bool(getattr(t, "synced_from_balance", False)):
                    continue
                if bool(getattr(t, "manual_picked", False)):
                    continue
                pos_qty = int(getattr(t, "pos_qty", 0) or 0)
                if pos_qty <= 0 and c not in seen_holding_codes:
                    stale_codes.append(c)
            if stale_codes:
                removed, _ = self._remove_trade_codes(
                    stale_codes,
                    reason="balance-stale-auto-exclude",
                    block_holding=True
                )
                if removed > 0:
                    self.log(f"[BAL] stale auto-excluded={removed}")

            if total_buy <= 0:
                total_buy = hold_buy
            if total_eval <= 0:
                total_eval = hold_eval
            if total_profit == 0 and total_buy > 0:
                total_profit = total_eval - total_buy

            self._update_account_pnl_ui(
                total_profit=int(total_profit),
                total_buy=int(total_buy),
                total_eval=int(total_eval),
            )
            self.log(
                f"[BAL] parsed holdings={synced} "
                f"total_buy={int(total_buy):,} total_eval={int(total_eval):,} "
                f"total_profit={int(total_profit):,}"
            )
        except Exception as e:
            self.log(f"[BAL] parse error: {e}")

    def preload_3m_bars(self, code):
        # 종목별 워밍업 완료 상태 맵
        self.warmup_ready[code] = False
        self.log(f"[WARMUP] preload 3m bars 요청: {code}")

        # 3분봉 선조회 워밍업
        # 3분봉 선조회 워밍업
        # 최근 100개 3분봉 선조회 요청
        self._req_3m_history(code, cnt=100)

    def _on_receive_tr_data(self, screenNo, rqname, trcode, recordName, prevNext, dataLen, errCode, msg1, msg2):
        rqname = str(rqname)
        self.log(f"[TR IN] rq={rqname} tr={str(trcode).strip()}")  # 수신 TR 기본 로그
        self.daily_sum.tr_in_total += 1

        tr = str(trcode).strip()
        if tr == "opt10001":
            self.daily_sum.tr_opt10001 += 1
        elif tr == "opt10081":
            self.daily_sum.tr_opt10081 += 1
        elif tr == "opt10080":
            self.daily_sum.tr_opt10080 += 1

        # 잔고 조회 TR(opw00018)은 별도 파싱 후 종료
        if rqname == "RQ_BAL_OPW00018" and tr == "opw00018":
            self._handle_balance_opw00018(trcode, recordName)
            return

        # opt10001 응답 파싱 분기
        if str(trcode).strip() == "opt10001":
            try:
                # TR 응답 타입별 파싱 분기
                # TR 응답 타입별 파싱 분기
                # 컨텍스트 코드와 응답 코드 매칭
                code_from_ctx = None
                if rqname in self.tr_wait:
                    code_from_ctx = self.tr_wait[rqname].get("code")

                code_data = str(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)",
                    trcode, recordName, 0, "종목코드"
                )).strip().replace("A", "").strip()

                code_real = code_from_ctx or code_data

                last = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, 0, "현재가"
                )))
                vol = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, 0, "거래량"
                )))
                prev_c = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, 0, "전일종가"
                )))
                today_o = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, 0, "시가"
                )))
                # TR 응답 타입별 파싱 분기
                self._render_candidates_topn()

                # Trade List 종목 값 업데이트
                if code_real in self.trade_map:
                    self._update_trade_row(code_real, last=last, vol=vol, today_o=today_o, prev_c=prev_c)

                # 후보 데이터(가격/거래량/대금) 갱신
                if code_real in self.candidates:
                    self.candidates[code_real]["price"] = last
                    self.candidates[code_real]["vol"] = vol
                    self.candidates[code_real]["value"] = last * vol
                    self._render_candidates_topn()  # 후보 테이블 화면 갱신
                self.log(f"[opt10001] {code_real} last={last} todayO={today_o} (prev via opt10081)")

            except Exception as e:
                self.log(f"[opt10001] parse error: {e}")
            if code_real in self.trade_map:
                t = self.trade_map[code_real]
                t.today_o = today_o
                t.prev_c = prev_c

            # 처리 완료한 TR 컨텍스트 제거
            self.tr_wait.pop(rqname, None)
            return

        ctx = self.tr_wait.get(rqname)
        if not ctx:
            # 컨텍스트 누락 TR 경고 집계
            self.daily_sum.tr_ctx_miss += 1
            self.daily_sum.warnings.append(f"ctx-miss rq={rqname} tr={str(trcode).strip()}")
            self.log(f"[TR IN] ctx-miss rq={rqname} tr={str(trcode).strip()}")  # 컨텍스트 누락 TR 로그
            return

        typ = ctx["type"]
        code = ctx["code"]

        if typ == "basic":
            try:
                price = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, 0, "현재가"
                )))
                vol = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, 0, "거래량"
                )))
                value = price * vol

                if code in self.candidates:
                    self.candidates[code]["price"] = price
                    self.candidates[code]["vol"] = vol
                    self.candidates[code]["value"] = value
                    self._render_candidates_topn()
            except Exception as e:
                self.log(f"[TR basic] parse error {code}: {e}")
        elif typ == "daily_prev":
            try:

                repeat = int(self.ocx.dynamicCall(
                    "GetRepeatCnt(QString, QString)", trcode, recordName
                ))

                if repeat < 2:
                    self.log(f"[TR daily_prev] {code} repeat<2")
                    return

                # 전일 시가/종가(1번째 일봉) 추출
                prev_o = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, 1, "시가"
                )))

                prev_c = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, 1, "현재가"
                )))

                if code in self.trade_map:
                    # 전일 OHLC를 테이블에 반영
                    self._update_trade_row(code, prev_o=prev_o, prev_c=prev_c)

                self.log(f"[TR daily_prev] {code} PrevO={prev_o} PrevC={prev_c}")
            except Exception as e:
                self.log(f"[TR daily_prev] parse error {code}: {e}")


        elif typ == "hist_3m":
            self._parse_tr_3m_bootstrap(trcode, recordName, code, ctx.get("cnt", 400))

        self.tr_wait.pop(rqname, None)


    def _parse_tr_3m_bootstrap(self, trcode: str, recordName: str, code: str, cnt: int):
        """
        3분봉 히스토리 부트스트랩(opt10080).
        - calc: 전일 12:00 이후 + 당일 (지표 계산용)
        - view: 전일 14:30 이후 + 당일 (차트 표시용)
        체결시간 포맷 차이 대응:
        - 케이스 A: 체결시간 14자리(YYYYMMDDHHMMSS)
        - 케이스 B: 일자(YYYYMMDD) + 체결시간(HHMMSS)
        """
        try:
            t = self.trade_map.get(code)
            if not t:
                return

            data_cnt = to_int(self.ocx.dynamicCall("GetRepeatCnt(QString, QString)", trcode, recordName))
            take = min(data_cnt, cnt)

            rows = []  # (date_str, time_str, epoch, close)
            for i in range(take):
                date_str = str(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, i, "일자"
                )).strip()

                time_str = str(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, i, "체결시간"
                )).strip()
                if not time_str:
                    time_str = str(self.ocx.dynamicCall(
                        "GetCommData(QString, QString, int, QString)", trcode, recordName, i, "시간"
                    )).strip()

                close_i = abs(to_int(self.ocx.dynamicCall(
                    "GetCommData(QString, QString, int, QString)", trcode, recordName, i, "현재가"
                )))
                if close_i <= 0:
                    continue

                epoch = None

                # 체결시간 14자리 포맷 파싱
                if time_str and len(time_str) >= 14 and time_str[:8].isdigit():
                    try:
                        y = int(time_str[0:4]);
                        mo = int(time_str[4:6]);
                        d = int(time_str[6:8])
                        hh = int(time_str[8:10]);
                        mm = int(time_str[10:12]);
                        ss = int(time_str[12:14])
                        epoch = int(datetime.datetime(y, mo, d, hh, mm, ss).timestamp())
                        date_str = f"{y:04d}{mo:02d}{d:02d}"
                        time_str = f"{hh:02d}{mm:02d}{ss:02d}"
                    except:
                        epoch = None

                # 일자+시간 조합 포맷 파싱
                if epoch is None and date_str and len(date_str) >= 8 and time_str and len(time_str) >= 6:
                    try:
                        y = int(date_str[0:4]);
                        mo = int(date_str[4:6]);
                        d = int(date_str[6:8])
                        hh = int(time_str[0:2]);
                        mm = int(time_str[2:4]);
                        ss = int(time_str[4:6])
                        epoch = int(datetime.datetime(y, mo, d, hh, mm, ss).timestamp())
                    except:
                        epoch = None

                rows.append((date_str, time_str, epoch, float(close_i)))

            if not rows:
                self.log(f"[TR 3m] {code} no rows")
                return

            # epoch 유효 데이터만 선별
            rows_epoch = [r for r in rows if r[2] is not None]
            if not rows_epoch:
                # epoch 부재 시 최근 종가로 폴백
                closes = [r[3] for r in rows][-300:]
                t.closes_3m = closes
                t.times_3m = list(range(len(closes)))
                # 계산/표시 버퍼를 동일 데이터로 정렬
                t.closes_3m_calc = closes[:]
                t.times_3m_calc = t.times_3m[:]
                self.log(f"[TR 3m] {code} fallback(no epoch): {len(closes)} bars (calc/view same)")
                if self.display_code == code:
                    self._refresh_charts()
                return

            rows_epoch.sort(key=lambda x: x[2])

            distinct_dates = sorted({r[0] for r in rows_epoch if r[0]})
            if len(distinct_dates) >= 2:
                today_date = distinct_dates[-1]
                prev_date = distinct_dates[-2]

                filtered_calc = []  # prev>=12:00 + today
                filtered_view = []  # prev>=14:30 + today

                for dstr, tstr, epoch, close_v in rows_epoch:
                    if dstr == today_date and "090000" <= tstr <= "153000":
                        filtered_calc.append((epoch, close_v))
                        filtered_view.append((epoch, close_v))

                    elif dstr == prev_date and "120000" <= tstr <= "153000":
                        filtered_calc.append((epoch, close_v))

                        # 표시용은 전일 14:30 이후 데이터만 포함
                        if "143000" <= tstr <= "153000":
                            filtered_view.append((epoch, close_v))

                if filtered_calc and filtered_view:
                    t.times_3m_calc = [e for e, _ in filtered_calc]
                    t.closes_3m_calc = [c for _, c in filtered_calc]

                    t.times_3m = [e for e, _ in filtered_view]
                    t.closes_3m = [c for _, c in filtered_view]

                    self.log(
                        f"[TR 3m] {code} bootstrap ok: "
                        f"calc={len(filtered_calc)}(prev>=12:00+today), "
                        f"view={len(filtered_view)}(prev>=14:30+today)"
                    )
                else:
                    # 필터 미충족 시 최근 300봉으로 폴백
                    closes = [r[3] for r in rows_epoch][-300:]
                    times_ = [r[2] for r in rows_epoch][-300:]
                    t.closes_3m = closes
                    t.times_3m = times_
                    t.closes_3m_calc = closes[:]
                    t.times_3m_calc = times_[:]
                    self.log(f"[TR 3m] {code} fallback(no date filter): {len(closes)} bars (calc/view same)")
            else:
                # 필터 미충족 시 최근 300봉으로 폴백
                closes = [r[3] for r in rows_epoch][-300:]
                times_ = [r[2] for r in rows_epoch][-300:]
                t.closes_3m = closes
                t.times_3m = times_
                t.closes_3m_calc = closes[:]
                t.times_3m_calc = times_[:]
                self.log(f"[TR 3m] {code} fallback(one date): calc/view same ({len(closes)} bars)")

            if self.display_code == code:
                self._refresh_charts()

            # =========================
            # 3분봉 부트스트랩 파싱/폴백 처리
            # =========================
            bars_len = len(t.closes_3m_calc) if getattr(t, "closes_3m_calc", None) else (
                len(t.closes_3m) if t.closes_3m else 0)

            if bars_len < 25:
                self.log(f"[WARMUP] bars insufficient({bars_len}). MACD/RSI 계산 보류: {code}")
                self.daily_sum.warmup_bars_insufficient += 1
                self.daily_sum._bump(code, "warmup_bars_insufficient", 1)
                return
            print("DISPLAY =", self.display_code, "WARMUP CODE =", code)
            # 지표 계산용 시계열 선택
            series = list(t.closes_3m_calc) if t.closes_3m_calc else list(t.closes_3m)

            r = rsi_series(series, self.rsi_period)
            t.rsi_3m = float(r[-1]) if r and r[-1] is not None else None

            mres = macd_series(series, self.macd_fast, self.macd_slow, self.macd_signal)
            if mres:
                m, s, h = mres
                t.macd_3m = float(m[-1] - s[-1])
            else:
                t.macd_3m = None

            self._update_trade_row(code, macd_3m=t.macd_3m, rsi_3m=t.rsi_3m)

            self.warmup_ready[code] = True
            self.log(f"[WARMUP] ready: {code} (calc_bars={len(t.closes_3m_calc)} / view_bars={len(t.closes_3m)})")
            self.daily_sum.warmup_ready_count += 1
            self.daily_sum._bump(code, "warmup_ready", 1)
            # 표시 종목이 없으면 현재 코드로 지정
            if not self.display_code:
                self.display_code = code
                print("AUTO SET DISPLAY =", code)

            if self.display_code == code:
                print("REFRESH AFTER WARMUP", code)
                self._refresh_charts()
        except Exception as e:
            self.log(f"[TR 3m] parse error {code}: {e}")

    def _set_realreg_for_trade_map(self):
        """
        trade_map 전체 종목을 최소 FID로 실시간 등록(현재가/거래량/체결시간).
        - 이미 등록되어 있어도 SetRealReg 재호출 가능하므로 screen 번호 관리에 주의
        """
        if not self.connected or not self.trade_map:
            return
        codes = ";".join(self.trade_map.keys())
        fid_list = "10;15;20"
        self.ocx.dynamicCall(
            "SetRealReg(QString, QString, QString, QString)",
            self.screen_real, codes, fid_list, "0"
        )
        self.log(f"[RealReg] synced codes={len(self.trade_map)}")

    def _drain_apply_tr_q(self):
        if not self.connected:
            self.apply_tr_timer.stop()
            return
        if not self.apply_tr_q:
            self.apply_tr_timer.stop()
            return

        fn, args = self.apply_tr_q.pop(0)
        try:
            fn(*args)
        except Exception as e:
            self.log(f"[APPLY_TR_Q] error: {e}")

    def calc_rsi_3m(self, code: str):
        t = self.trade_map.get(code)
        if not t or not getattr(t, "closes_3m", None):
            return
        r = rsi_series(list(t.closes_3m), self.rsi_period)
        if r and r[-1] is not None:
            t.rsi_3m = float(r[-1])

    def calc_macd_3m(self, code: str):
        t = self.trade_map.get(code)
        if not t or not getattr(t, "closes_3m", None):
            return
        mres = macd_series(list(t.closes_3m), self.macd_fast, self.macd_slow, self.macd_signal)
        if mres:
            m, s, h = mres
            t.macd_3m = float(m[-1]) if m and m[-1] is not None else None
            t.macd_sig_3m = float(s[-1]) if s and s[-1] is not None else None
            t.macd_hist_3m = float(h[-1]) if h and h[-1] is not None else None

    # 지표 계산 호환 래퍼
    def _calc_rsi_3m(self, code: str):
        return self.calc_rsi_3m(code)

    def _calc_macd_3m(self, code: str):
        return self.calc_macd_3m(code)

    def _tr_housekeeping(self):
        if len(self.tr_wait) > 10:
            self.log(f"[WARN] TR pending too many: {len(self.tr_wait)}")

    # =========================
    # BUY DEBUG LOG
    # =========================
    def _debug_buy_state(self, code, price, rsi, macd, pos_qty):

        reason = []

        if not self.warmup_ready.get(code, False):
            reason.append("warmup_not_ready")

        if rsi is None:
            reason.append("rsi_none")
        elif rsi >= self.rsi_buy_level:
            reason.append(f"rsi_high({rsi:.2f})")

        if macd is None:
            reason.append("macd_none")

        if pos_qty > 0:
            reason.append("already_holding")

        if not self.arm_live:
            reason.append("arm_live_off")

        if reason:
            self.log(f"[BUY-BLOCK] {code} -> {', '.join(reason)}")
        else:
            self.log(f"[BUY-READY] {code} rsi={rsi:.2f} macd={macd:.2f}")

    # =========================
    # SELL DEBUG LOG
    # =========================
    def _debug_sell_state(self, code, price, rsi, macd, pos_qty):

        if pos_qty <= 0:
            return

        reason = []

        if rsi is not None and rsi < 70:
            reason.append(f"rsi_not_overbought({rsi:.2f})")

        if macd is not None and macd > 0:
            reason.append(f"macd_positive({macd:.2f})")

        if reason:
            self.log(f"[SELL-HOLD] {code} -> {', '.join(reason)}")
        else:
            self.log(f"[SELL-READY] {code}")
    # -------------------------
    # Real-time data: build 3s + update 3m
    # -------------------------
    def _on_receive_real_data(self, code, realType, realData):

        code = str(code).strip()
        if code not in self.trade_map:
            return
        if str(realType) != "주식체결":
            return

        last = abs(to_int(self.ocx.dynamicCall("GetCommRealData(QString, int)", code, 10)))
        vol = abs(to_int(self.ocx.dynamicCall("GetCommRealData(QString, int)", code, 15)))
        tstr = str(self.ocx.dynamicCall("GetCommRealData(QString, int)", code, 20)).strip()

        if last <= 0:
            return

        t = self.trade_map[code]

        # 실시간 체결 수신 후 지표/UI 갱신
        t.last = last
        t.vol = vol
        self._update_trade_row(code, last=last, vol=vol)
        self._update_account_summary_program()
        ts = self._to_epoch_seconds(tstr)

        # 실시간 체결 수신 후 지표/UI 갱신
        self._update_3s_candle(code, ts, last)

        # 실시간 체결 수신 후 지표/UI 갱신
        finalized = self._update_3m_from_ticks(code, ts, last)

        # 실시간 체결 수신 후 지표/UI 갱신
        macd_last, sig_last, rsi_last = self._compute_indicators_3m(t)

        # 실시간 체결 수신 후 지표/UI 갱신
        if not hasattr(t, "rsi_hist_3m") or t.rsi_hist_3m is None:
            t.rsi_hist_3m = []

        if rsi_last is not None:
            t.rsi_hist_3m.append(rsi_last)
            if len(t.rsi_hist_3m) > 5:
                t.rsi_hist_3m = t.rsi_hist_3m[-5:]

        # 실시간 체결 수신 후 지표/UI 갱신
        self._update_trade_row(code, macd_3m=macd_last, rsi_3m=rsi_last)

        # =========================
        # NEW BUY1 / BUY2 LOGIC
        # =========================

        # 실시간 체결 수신 후 지표/UI 갱신
        series, _ = self._series_3m(t)

        if datetime.datetime.now().time() >= datetime.time(15, 0):
            return

        if rsi_last is None or len(series) < 10:
            return

        prev_rsi = getattr(t, "prev_rsi_last", rsi_last)
        close_now = series[-1]
        prev_close = series[-2]

        ema5 = ema_series(series, 5)
        ema10 = ema_series(series, 10)
        ema20 = ema_series(series, 20)
        ema20_last = ema20[-1] if ema20 else None
        ema5_last = ema5[-1] if ema5 else None
        ema10_last = ema10[-1] if ema10 else None

        # 실시간 체결 수신 후 지표/UI 갱신
        t.prev_rsi_last = rsi_last

        # 실시간 체결 수신 후 지표/UI 갱신
        if self.display_code == code:
            self._refresh_charts()

    def _to_epoch_seconds(self, hhmmss: str) -> int:
        try:
            if hhmmss and len(hhmmss) >= 6:
                h = int(hhmmss[0:2]); m = int(hhmmss[2:4]); s = int(hhmmss[4:6])
                now = datetime.datetime.now()
                dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
                return int(dt.timestamp())
        except:
            pass
        return int(datetime.datetime.now().timestamp())

    def _bucket_start(self, epoch_sec: int, bucket_sec: int) -> int:
        return epoch_sec - (epoch_sec % bucket_sec)

    def _update_3s_candle(self, code: str, epoch_sec: int, price: int):
        t = self.trade_map.get(code)
        if not t:
            return

        bucket = self._bucket_start(epoch_sec, self.candle_sec)
        c = t.cur_3s

        if c is None:
            t.cur_3s = Candle(start_ts=bucket, o=price, h=price, l=price, c=price)
            return

        if bucket == c.start_ts:
            c.h = max(c.h, price)
            c.l = min(c.l, price)
            c.c = price
            return

        prev = c
        t.candles_3s.append(prev)
        t.closes_3s.append(float(prev.c))

        missing = (bucket - prev.start_ts) // self.candle_sec
        if missing > 1:
            for i in range(1, missing):
                filler_ts = prev.start_ts + i * self.candle_sec
                filler_price = prev.c
                t.candles_3s.append(Candle(start_ts=filler_ts, o=filler_price, h=filler_price, l=filler_price, c=filler_price))
                t.closes_3s.append(float(filler_price))

        if len(t.candles_3s) > self.max_candles_3s:
            cut = len(t.candles_3s) - self.max_candles_3s
            t.candles_3s = t.candles_3s[cut:]
        if len(t.closes_3s) > self.max_candles_3s:
            cut = len(t.closes_3s) - self.max_candles_3s
            t.closes_3s = t.closes_3s[cut:]

        t.cur_3s = Candle(start_ts=bucket, o=price, h=price, l=price, c=price)

    def _update_3m_from_ticks(self, code: str, epoch_sec: int, price: int) -> bool:
        """
        실시간 틱으로 3분봉을 이어붙임.
        return:
            True  -> 직전 3분봉이 방금 확정됨
            False -> 아직 진행 중
        """
        t = self.trade_map.get(code)
        if not t:
            return False

        bucket = self._bucket_start(epoch_sec, 180)

        # 첫 3분 버킷 초기화
        if t.cur_3m_bucket == 0:
            t.cur_3m_bucket = bucket
            t.cur_3m_close = float(price)
            return False

        # 동일 버킷에서는 종가만 갱신
        if bucket == t.cur_3m_bucket:
            t.cur_3m_close = float(price)
            return False

        # 틱 기반 3분봉 확정/보간/길이 관리

        # 완료된 3분봉을 시계열에 확정 반영
        t.times_3m.append(t.cur_3m_bucket)
        t.closes_3m.append(t.cur_3m_close)
        # 계산용 버퍼 누락 시 보정 생성
        if not hasattr(t, "times_3m_calc") or t.times_3m_calc is None:
            t.times_3m_calc = []
        if not hasattr(t, "closes_3m_calc") or t.closes_3m_calc is None:
            t.closes_3m_calc = []
        t.times_3m_calc.append(t.cur_3m_bucket)
        t.closes_3m_calc.append(t.cur_3m_close)

        # 빈 구간(gap) 보간 처리
        gap = (bucket - t.cur_3m_bucket) // 180
        if gap > 1:
            for i in range(gap - 1):
                fill_ts = t.cur_3m_bucket + 180 * (i + 1)
                t.times_3m.append(fill_ts)
                t.closes_3m.append(t.cur_3m_close)
        # 보간 데이터도 계산용 버퍼에 동기화
                t.times_3m_calc.append(fill_ts)
                t.closes_3m_calc.append(t.cur_3m_close)
        # 새 3분 버킷으로 롤오버
        t.cur_3m_bucket = bucket
        t.cur_3m_close = float(price)
        # 손절 후 재진입 대기 카운트 관리
        if t.stop1_done and t.stop1_wait_bars > 0:
            t.stop1_wait_bars -= 1
            self.log(f"[WAIT DOWN] {code} remaining_wait={t.stop1_wait_bars}")

            if t.stop1_wait_bars == 0:
                t.stop1_done = False
                self.log(f"[WAIT END] {code} re-entry allowed")
        # 표시용 3분봉 길이 제한
        if len(t.closes_3m) > self.max_bars_3m:
            cut = len(t.closes_3m) - self.max_bars_3m
            t.closes_3m = t.closes_3m[cut:]
            t.times_3m = t.times_3m[cut:]
        # 계산용 3분봉 길이 제한
        MAX_CALC = getattr(self, "max_bars_3m_calc", 600)
        if len(t.closes_3m_calc) > MAX_CALC:
            cut2 = len(t.closes_3m_calc) - MAX_CALC
            t.closes_3m_calc = t.closes_3m_calc[cut2:]
            t.times_3m_calc = t.times_3m_calc[cut2:]
        return True

    # -------------------------
    # Indicators (3m): MACD + RSI
    # -------------------------
    def _compute_indicators_3m(self, t: TradeItem):
        # -------------------------
        # MACD/RSI 계산
        # MACD/RSI 계산
        # -------------------------
        series_macd = t.closes_3m_calc[:] if getattr(t, "closes_3m_calc", None) else t.closes_3m[:]

        macd_last = sig_last = None
        rsi_last = None

        mres = macd_series(series_macd, self.macd_fast, self.macd_slow, self.macd_signal)
        if mres is not None:
            m, s, h = mres
            macd_last = m[-1]
            sig_last = s[-1]

        # -------------------------
        # MACD/RSI 계산
        # -------------------------
        series_rsi = t.closes_3m_calc[:] if getattr(t, "closes_3m_calc", None) else t.closes_3m[:]

        r = rsi_series(series_rsi, self.rsi_period)
        if r and r[-1] is not None:
            rsi_last = float(r[-1])

        return macd_last, sig_last, rsi_last

    def _series_3m(self, t: TradeItem) -> Tuple[List[float], List[int]]:
        series = t.closes_3m[:]
        x_series = t.times_3m[:]
        if t.cur_3m_bucket != 0:
            series = series + [t.cur_3m_close]
            x_series = x_series + [t.cur_3m_bucket]
        return series, x_series

    def _ensure_charts(self):

        if hasattr(self, "charts_ready") and self.charts_ready:
            return

        if not hasattr(self, "chart_widget") or self.chart_widget is None:
            return

        self.chart_widget.clear()

        # =========================
        # PRICE
        # =========================
        self.price_axis = HTSAxis('bottom')

        self.price_plot = self.chart_widget.addPlot(
            row=0, col=0,
            axisItems={'bottom': self.price_axis}
        )
        self.price_plot.setTitle("PRICE (3m)")
        self.price_plot.showGrid(x=False, y=True)

        self.price_curve = pg.PlotCurveItem(
            pen=pg.mkPen(color=(200, 200, 200, 120), width=1)
        )
        self.price_plot.addItem(self.price_curve)

        # =========================
        # RSI
        # =========================
        self.chart_widget.nextRow()

        self.rsi_axis = HTSAxis('bottom')

        self.rsi_plot = self.chart_widget.addPlot(
            axisItems={'bottom': self.rsi_axis}
        )
        self.rsi_plot.setTitle("RSI (3m)")
        self.rsi_plot.setYRange(0, 100)
        self.rsi_plot.showGrid(x=False, y=True)

        # RSI 라인 커브 생성
        self.rsi_curve = self.rsi_plot.plot(
            pen=pg.mkPen(color=(0, 150, 255), width=2)
        )

        # RSI 기준선(50) 추가
        self.rsi_plot.addItem(pg.InfiniteLine(
            pos=50,
            angle=0,
            pen=pg.mkPen((120, 120, 120), style=Qt.DotLine)
        ))

        # RSI 패널 우측 가격 오버레이 뷰 생성
        self.rsi_price_vb = pg.ViewBox()
        self.rsi_plot.showAxis('right')
        self.rsi_plot.scene().addItem(self.rsi_price_vb)
        self.rsi_plot.getAxis('right').linkToView(self.rsi_price_vb)
        self.rsi_price_vb.setXLink(self.rsi_plot)

        self.rsi_price_curve = pg.PlotCurveItem(
            pen=pg.mkPen(color=(200, 200, 200, 120), width=1)
        )
        self.rsi_price_vb.addItem(self.rsi_price_curve)

        def update_rsi_views():
            self.rsi_price_vb.setGeometry(
                self.rsi_plot.getViewBox().sceneBoundingRect()
            )
            self.rsi_price_vb.linkedViewChanged(
                self.rsi_plot.getViewBox(),
                self.rsi_price_vb.XAxis
            )

        self.rsi_plot.getViewBox().sigResized.connect(update_rsi_views)

        # =========================
        # MACD
        # =========================
        self.chart_widget.nextRow()

        self.macd_axis = HTSAxis('bottom')

        self.macd_plot = self.chart_widget.addPlot(
            axisItems={'bottom': self.macd_axis}
        )
        self.macd_plot.setTitle("MACD (3m)")
        self.macd_plot.showGrid(x=False, y=True)

        self.macd_curve = self.macd_plot.plot(pen=pg.mkPen(width=2))
        self.signal_curve = self.macd_plot.plot(pen=pg.mkPen(width=2, style=Qt.DashLine))

        self.macd_zero = pg.InfiniteLine(pos=0, angle=0,
                                         pen=pg.mkPen((200, 200, 200)))
        self.macd_plot.addItem(self.macd_zero)

        self.macd_hist_item = None

        self.charts_ready = True
    # -------------------------
    # RSI 보조 뷰 동기화
    # -------------------------
    def _refresh_charts(self):

        code = self.display_code
        if code in self.trade_map:
            t = self.trade_map[code]
            name = getattr(t, "name", "")
        else:
            name = ""

        self.price_plot.setTitle(f"PRICE (3m) | {code} {name}")
        self.rsi_plot.setTitle(f"RSI (3m) | {code} {name}")
        self.macd_plot.setTitle(f"MACD (3m) | {code} {name}")
        if not code or code not in self.trade_map:
            return

        t = self.trade_map[code]

        series = list(t.closes_3m) if t.closes_3m else []
        times = list(t.times_3m) if t.times_3m else []

        if len(series) < 2:
            return

        # =========================
        # 표시 종목 차트 갱신
        # =========================
        x = list(range(len(series)))
        dt_list = [datetime.datetime.fromtimestamp(e) for e in times]

        # =========================
        # PRICE
        # =========================
        self.price_curve.setData(x, series)
        self.price_axis.set_datetimes(dt_list)

        # EMA
        ema5 = ema_series(series, 5)
        ema10 = ema_series(series, 10)

        if hasattr(self, "ema5_curve"):
            self.price_plot.removeItem(self.ema5_curve)
        if hasattr(self, "ema10_curve"):
            self.price_plot.removeItem(self.ema10_curve)

        if ema5:
            self.ema5_curve = self.price_plot.plot(x, ema5,
                                                   pen=pg.mkPen((0, 120, 255), width=2))
        if ema10:
            self.ema10_curve = self.price_plot.plot(x, ema10,
                                                    pen=pg.mkPen((255, 0, 0), width=2))

        # =========================
        # RSI
        # =========================
        if len(series) >= self.rsi_period + 2:

            r = rsi_series(series, self.rsi_period)
            yr_clean = [v for v in r if v is not None]

            if len(yr_clean) > 5:
                x_clean = x[-len(yr_clean):]
                self.rsi_curve.setData(x_clean, yr_clean)
               # RSI 패널에 가격 오버레이 갱신
                self.rsi_price_curve.setData(x, series)
                self.rsi_axis.set_datetimes(dt_list)

                # RSI 패널용 EMA60 계산
                ema60 = ema_series(series, 60)

                if hasattr(self, "rsi_ema_curve"):
                    self.rsi_plot.removeItem(self.rsi_ema_curve)

                if ema60 and len(ema60) > 5:
                    x_ema60 = x[-len(ema60):]

                    self.rsi_ema_curve = self.rsi_plot.plot(
                        x_ema60,
                        ema60,
                        pen=pg.mkPen((255, 165, 0), width=2)
                    )
                    # EMA60을 우측 가격축에 오버레이
                    self.rsi_price_vb.addItem(self.rsi_ema_curve)
        # =========================
        # MACD
        # =========================
        if len(series) >= self.macd_slow + 5:

            res = macd_series(series, self.macd_fast, self.macd_slow, self.macd_signal)
            if res:

                m, s, h = res
                x_macd = list(range(len(m)))

                self.macd_curve.setData(x_macd, m)
                self.signal_curve.setData(x_macd, s)
                self.macd_axis.set_datetimes(dt_list[-len(m):])

                brushes = [
                    pg.mkBrush(100, 200, 100, 160) if hv >= 0
                    else pg.mkBrush(220, 100, 100, 160)
                    for hv in h
                ]

                if self.macd_hist_item:
                    self.macd_plot.removeItem(self.macd_hist_item)

                self.macd_hist_item = pg.BarGraphItem(
                    x=x_macd,
                    height=h,
                    width=0.6,
                    brushes=brushes
                )

                self.macd_plot.addItem(self.macd_hist_item)
    def _safe_int(self, value):
        try:
            if value is None:
                return 0
            value = str(value).strip().replace(",", "")
            if value == "":
                return 0
            return int(float(value))
        except:
            return 0

    def _get_comm_data_safe(self, trcode, recordName, index, field):
        try:
            data = self.ocx.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                trcode, recordName, index, field
            )
            return str(data).strip()
        except:
            return ""

    def _reset_position_state(self, t):
        t.pos_qty = 0
        t.avg_price = 0
        t.used_budget = 0
        t.daily_entry_count = 0
        t.stage = 0
        t.signal = "WAIT"
    # -------------------------
    # 체결/잔고(Chejan) 헬퍼
    # -------------------------
    def _get_chejan(self, fid: int) -> str:
        return self.ocx.dynamicCall("GetChejanData(int)", fid)

    def _on_receive_chejan_data(self, gubun, itemCnt, fidList):

        gubun = str(gubun).strip()
        code = clean_code(self._get_chejan(CHEJAN_FID["code"]))
        if not code:
            return

        # 체결 코드 자동 등록(조건 강화)
        if code not in self.trade_map:
            should_create = False
            if gubun == "0":
                status_probe = self._get_chejan(CHEJAN_FID["order_status"]).strip()
                fill_qty_probe = to_int(self._get_chejan(CHEJAN_FID["filled_qty"]))
                fill_px_probe = abs(to_int(self._get_chejan(CHEJAN_FID["filled_price"])))
                should_create = (status_probe == "체결" and fill_qty_probe > 0 and fill_px_probe > 0)
            elif gubun == "1":
                pos_qty_probe = to_int(self._get_chejan(CHEJAN_FID["pos_qty"]))
                should_create = (pos_qty_probe > 0)

            if not should_create:
                self.log(f"[CHEJAN] skip auto-create code={code} gubun={gubun}")
                return

            self.trade_map[code] = TradeItem(code=code)
            self.trade_map[code].auto_created_by_chejan = True
            self.trade_map[code].manual_picked = False
            self.trade_map[code].synced_from_balance = False
            self.log(f"[CHEJAN] auto-create trade_map item for {code} (gubun={gubun})")
            self._render_trade_list()

        t = self.trade_map[code]

        # =====================================================
        # 체결/잔고 이벤트 반영
        # =====================================================
        if gubun == "0":

            status = self._get_chejan(CHEJAN_FID["order_status"]).strip()
            filled_qty = to_int(self._get_chejan(CHEJAN_FID["filled_qty"]))
            filled_price = abs(to_int(self._get_chejan(CHEJAN_FID["filled_price"])))
            unfilled = to_int(self._get_chejan(CHEJAN_FID["unfilled_qty"]))
            order_gubun = self._get_chejan(CHEJAN_FID["order_gubun"]).strip()

            self.log(
                f"[CHEJAN-0] {code} status={status} "
                f"fill_qty={filled_qty} fill_px={filled_price} unfilled={unfilled}"
            )

            # 실제 체결 건만 반영
            if status != "체결" or filled_qty <= 0:
                return

            # =========================
            # 체결/잔고 이벤트 반영
            # =========================
            if "매수" in order_gubun:

                total_cost = filled_price * filled_qty

                # 가중평균 단가 재계산
                new_qty = t.pos_qty + filled_qty
                if new_qty > 0:
                    t.avg_price = (
                            (t.avg_price * t.pos_qty + total_cost)
                            / new_qty
                    )

                t.pos_qty = new_qty
                t.used_budget += total_cost
                t.daily_entry_count += 1

                self.today_total_spent += total_cost

                self.log(
                    f"[FILLED BUY] {code} qty={filled_qty} "
                    f"pos={t.pos_qty} avg={t.avg_price:.2f} "
                    f"used={t.used_budget:,}"
                )

            # =========================
            # 체결/잔고 이벤트 반영
            # =========================
            elif "매도" in order_gubun:

                realized = int((filled_price - t.avg_price) * filled_qty)

                t.pos_qty -= filled_qty
                t.used_budget -= int(t.avg_price * filled_qty)

                if t.used_budget < 0:
                    t.used_budget = 0

                self.today_total_realized += realized

                self.log(
                    f"[FILLED SELL] {code} qty={filled_qty} "
                    f"realized={realized:,} remain_pos={t.pos_qty} "
                    f"used={t.used_budget:,}"
                )

                if t.pos_qty <= 0:
                    self._reset_position_state(t)
                    if not bool(getattr(t, "manual_picked", False)):
                        self._remove_trade_codes([code], reason="chejan-qty0-auto-exclude", block_holding=False)
                        self.log(f"[CHEJAN] auto-excluded qty=0 code={code}")
                        return
            t.pending_buy = False
            t.pending_sell = False

            # 체결 결과를 UI/모델에 동기화
            self._update_trade_row(
                code,
                pos_qty=t.pos_qty,
                avg_price=t.avg_price,
                stage=t.stage,
                signal=t.signal
            )

            return  # 체결 처리 후 함수 종료


        # =====================================================
        # 체결/잔고 이벤트 반영
        # =====================================================
        elif gubun == "1":

            pos_qty = to_int(self._get_chejan(CHEJAN_FID["pos_qty"]))
            avg_price = to_float(self._get_chejan(CHEJAN_FID["avg_price"]))

            # 잔고통보 수량/평단으로 덮어쓰기
            t.pos_qty = pos_qty
            t.avg_price = avg_price
            if pos_qty <= 0 and not bool(getattr(t, "manual_picked", False)):
                self._remove_trade_codes([code], reason="chejan-qty0-auto-exclude", block_holding=False)
                self.log(f"[CHEJAN] auto-excluded qty=0 code={code}")
                return

            self.log(
                f"[CHEJAN-1 SYNC] {code} pos_qty={pos_qty} avg={avg_price:.2f}"
            )

            self._update_trade_row(
                code,
                pos_qty=pos_qty,
                avg_price=avg_price,
                stage=t.stage,
                signal=t.signal
            )
            self.log(
                f"[CHEJAN-1] {code} pos_qty={pos_qty} "
                f"avg_price={avg_price:.2f} "
                f"today_total_spent={self.today_total_spent:,} "
                f"today_total_realized={self.today_total_realized:,}"
            )
    # -------------------------
    # Strategy loop
    # -------------------------
    def _on_strategy_tick(self):

        if not self.trade_map:
            return

        self._refresh_market_regime()
        self._enforce_daily_loss_limit()

        for code, t in self.trade_map.items():

            # 청산 조건 우선 점검
            self._check_exit_strategy(code, t)

            # 장마감 청산 규칙 점검
            self._check_eod_profit_exit_or_hold(code, t)

            # 자동매매 ON + 리스크 통과 시 진입 평가
            if self.auto_trade_enabled and not self.daily_loss_hit:
                self._check_entry_condition_3m(code, t)
            # 필요 시 추가 손절 로직 연결 지점
            # self._check_stoploss_prev_oc(code, t)
    # -------------------------
    # Entry: condition list + 3m RSI
    # -------------------------
    def _check_buy_rsi_only(self, t) -> bool:
        """
        RSI 단독 매수 판단(확정 3분봉 기준)
        """
        BUY_RSI = self.rsi_buy_level


        # 보유 중이면 신규 진입 금지
        if t.pos_qty > 0:
            return False

        rsis = getattr(t, "rsi_hist_3m", [])
        if len(rsis) < 3:
            return False

        rsi_2ago = rsis[-3]
        rsi_1ago = rsis[-2]
        rsi_now = rsis[-1]

        # 현재 RSI가 기준 이상이면 진입 보류
        if rsi_now > BUY_RSI:
            return False

        # RSI 반등 패턴 확인
        if not (rsi_1ago <= rsi_2ago and rsi_now >= rsi_1ago):
            return False

        return True

    def _check_entry_condition_3m(self, code: str, t: TradeItem):
        # =========================
        # [AUTO TRADE GUARD]
        # =========================
        if self.daily_loss_hit:
            return

        # 15:15 이후 신규 진입 차단
        if datetime.datetime.now().time() >= datetime.time(15, 15):
            return

        if self.is_warmup:
            return
        if not self.auto_trade_enabled:
            return
        if not self.warmup_ready.get(code, False):
            return

        # 확정 3분봉 시계열 확보
        series, _ = self._series_3m(t)
        if len(series) < self.macd_slow + self.macd_signal + 2:
            return
        if t.last <= 0:
            return

        mres = macd_series(series, self.macd_fast, self.macd_slow, self.macd_signal)
        if mres is None:
            return
        m, s, h = mres

        r = rsi_series(series, self.rsi_period)
        if not r or r[-1] is None:
            return
        rsi_last = float(r[-1])
        # 최신 MACD/RSI를 모델에 저장
        t.macd_3m = float(m[-1] - s[-1])  # 최신 MACD(선-시그널) 저장
        t.rsi_3m = float(rsi_last)
        self._update_trade_row(code, macd_3m=t.macd_3m, rsi_3m=t.rsi_3m)

        cur_bucket = t.cur_3m_bucket if t.cur_3m_bucket else self._bucket_start(
            int(datetime.datetime.now().timestamp()), 180)

        per_stock = self._per_stock_budget()
        if per_stock <= 0:
            return

        # =========================
        # 3분봉 진입 조건 평가
        # =========================
        close_now = series[-1]
        prev_close = series[-2]
        prev_rsi = r[-2]

        ema20 = ema_series(series, 20)
        ema60 = ema_series(series, 60)

        if not ema20 or not ema60:
            return

        ema20_last = ema20[-1]
        ema20_prev = ema20[-2]
        ema60_last = ema60[-1]

        # =========================
        # BUY1
        # =========================
        buy1_triggered = False

        if close_now > ema60_last and ema60[-1] > ema60[-2]:
            if h[-1] > 0:
                if rsi_last <= self.rsi_buy_level:
                    t.rsi_was_below = True

                cross_up = (prev_rsi <= self.rsi_buy_level and rsi_last > self.rsi_buy_level)

                if getattr(t, "rsi_was_below", False) and cross_up:
                    buy1_triggered = True

        if buy1_triggered:
            if self._is_regime_blocked("BUY1_PULLBACK"):
                self._log_risk_once(
                    f"regime_buy1_{code}",
                    f"[ENTRY BLOCK][REGIME] {code} BUY1 blocked (regime={self.market_regime}, mode={self.regime_filter_mode})",
                )
                return
            remaining_budget = per_stock - t.used_budget
            if remaining_budget > 0:
                budget = int(remaining_budget * 0.3)
                est_buy_px = self._apply_slippage(t.last, "BUY")
                qty = budget // max(1, est_buy_px)

                if qty > 0:
                    self._buy_market(code, qty, reason="BUY1_PULLBACK")
                    t.rsi_was_below = False

        # =========================
        # BUY2
        # =========================
        buy2_triggered = False

        ema20_break = (prev_close <= ema20_prev and close_now > ema20_last)
        rsi_break_50 = (prev_rsi <= 50 and rsi_last > 50)
        macd_positive = (h[-1] > 0)

        if ema20_break and rsi_break_50 and macd_positive:
            buy2_triggered = True

        if buy2_triggered:
            if self._is_regime_blocked("BUY2_BREAKOUT"):
                self._log_risk_once(
                    f"regime_buy2_{code}",
                    f"[ENTRY BLOCK][REGIME] {code} BUY2 blocked (regime={self.market_regime}, mode={self.regime_filter_mode})",
                )
                return
            remaining_budget = per_stock - t.used_budget
            if remaining_budget > 0:
                budget = int(remaining_budget * 0.5)
                est_buy_px = self._apply_slippage(t.last, "BUY")
                qty = budget // max(1, est_buy_px)

                if qty > 0:
                    self._buy_market(code, qty, reason="BUY2_BREAKOUT")
    def _print_daily_summary(self):
        txt = self.daily_sum.render(
            arm_live=self.arm_live,
            today_spent=int(getattr(self, "today_spent", 0)),
            daily_budget=int(getattr(self, "daily_budget", 0)),
        )

        # 요약 전문을 로그창에 출력
        self.log(txt)

        # 요약 텍스트/CSV 파일 저장
        self._save_daily_summary_txt(txt)
        self._save_daily_summary_csv()

        self._save_daily_stats_enhanced()

    def _save_daily_performance(self):
        today = datetime.datetime.now().strftime("%Y-%m-%d")

        buy = self.perf_today["buy"]
        sell = self.perf_today["sell"]
        pnl = self.perf_today["realized"]
        trades = self.perf_today["trades"]

        win_rate = 0
        if trades > 0:
            win_rate = (1 if pnl > 0 else 0) * 100

        line = f"{today},{buy},{sell},{pnl},{trades}\n"

        path = os.path.join("logs", "performance_history.csv")
        os.makedirs("logs", exist_ok=True)

        # 일별 성과를 히스토리 CSV에 누적
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

        self.log(f"[PERF] daily saved -> {path}")

    def _generate_performance_report(self):
        path = os.path.join("logs", "performance_history.csv")
        if not os.path.exists(path):
            return

        df = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                date, buy, sell, pnl, trades = line.strip().split(",")
                df.append({
                    "date": date,
                    "buy": int(buy),
                    "sell": int(sell),
                    "pnl": int(pnl),
                    "trades": int(trades)
                })

        pdf = pd.DataFrame(df)
        pdf["date"] = pd.to_datetime(pdf["date"])

        daily = pdf.groupby("date").sum()
        weekly = pdf.groupby(pd.Grouper(key="date", freq="W")).sum()
        monthly = pdf.groupby(pd.Grouper(key="date", freq="M")).sum()
        yearly = pdf.groupby(pd.Grouper(key="date", freq="Y")).sum()

        report_path = os.path.join("logs", "performance_report.txt")

        with open(report_path, "w", encoding="utf-8") as f:
            f.write("===== DAILY =====\n")
            f.write(str(daily))
            f.write("\n\n===== WEEKLY =====\n")
            f.write(str(weekly))
            f.write("\n\n===== MONTHLY =====\n")
            f.write(str(monthly))
            f.write("\n\n===== YEARLY =====\n")
            f.write(str(yearly))

        self.log(f"[PERF] full report saved -> {report_path}")

    def _save_daily_summary_txt(self, text: str):
        base_dir = os.path.join(os.getcwd(), "logs", "daily_summary")
        os.makedirs(base_dir, exist_ok=True)

        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        fname = f"{date_str}_summary.txt"
        fpath = os.path.join(base_dir, fname)

        try:
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(text)
            self.log(f"[SUMMARY] txt saved: {fpath}")
        except Exception as e:
            self.log(f"[SUMMARY] txt save error: {e}")

    def _save_daily_summary_csv(self):
        base_dir = os.path.join(os.getcwd(), "logs", "daily_summary")
        os.makedirs(base_dir, exist_ok=True)

        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        fname = f"{date_str}_summary.csv"
        fpath = os.path.join(base_dir, fname)

        try:
            with open(fpath, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)

                # CSV 헤더 작성
                writer.writerow(["category", "key", "value"])

                s = self.daily_sum

                # Setup
                writer.writerow(["setup", "apply_picked_count", s.apply_picked_count])
                writer.writerow(["setup", "realreg_code_count", s.realreg_code_count])

                # TR
                writer.writerow(["tr", "total", s.tr_in_total])
                writer.writerow(["tr", "opt10001", s.tr_opt10001])
                writer.writerow(["tr", "opt10081", s.tr_opt10081])
                writer.writerow(["tr", "opt10080", s.tr_opt10080])
                writer.writerow(["tr", "ctx_miss", s.tr_ctx_miss])

                # Warmup
                writer.writerow(["warmup", "ready_count", s.warmup_ready_count])
                writer.writerow(["warmup", "bars_insufficient", s.warmup_bars_insufficient])

                # Signals
                writer.writerow(["signal", "entry", s.entry_signals])
                writer.writerow(["signal", "add", s.add_signals])

                # Orders
                writer.writerow(["order", "buy_live", s.buy_attempts_live])
                writer.writerow(["order", "buy_sim", s.buy_attempts_sim])
                writer.writerow(["order", "sell_live", s.sell_attempts_live])
                writer.writerow(["order", "sell_sim", s.sell_attempts_sim])

                # Budget
                writer.writerow(["budget", "block_count", s.buy_block_budget])
                writer.writerow(["budget", "spent", self.today_spent])
                writer.writerow(["budget", "limit", self.daily_budget])

                # 코드별 통계는 상위 일부만 요약 출력
                for code, stats in s.per_code.items():
                    for k, v in stats.items():
                        writer.writerow(["per_code", f"{code}:{k}", v])

            self.log(f"[SUMMARY] csv saved: {fpath}")
        except Exception as e:
            self.log(f"[SUMMARY] csv save error: {e}")

    def _save_daily_stats_enhanced(self):
        """
        고도화 통계 저장
        - unrealized(평가손익): trade_map 기준 합산
        - realized(실현손익): realized_pnl_today 기준
        - total = realized + unrealized
        """
        base_dir = os.path.join(os.getcwd(), "logs", "daily_summary")
        os.makedirs(base_dir, exist_ok=True)

        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        fpath = os.path.join(base_dir, f"{date_str}_stats.csv")

        unreal_pnl, unreal_pct, eval_value = self._calc_account_totals_from_trade_map()
        realized = 0
        for code, t in self.trade_map.items():
            realized += int(getattr(t, "realized_pnl_today", 0) or 0)

        total = int(realized + unreal_pnl)

        write_header = not os.path.exists(fpath)
        try:
            with open(fpath, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if write_header:
                    w.writerow(["date", "unrealized_pnl", "unrealized_pct", "eval_value", "realized_pnl", "total_pnl"])
                w.writerow(
                    [date_str, int(unreal_pnl), f"{float(unreal_pct):.2f}", int(eval_value), int(realized), int(total)])
            self.log(f"[STATS] enhanced saved: {fpath}")
        except Exception as e:
            self.log(f"[STATS] enhanced save error: {e}")

        now = datetime.datetime.now().time()
        if now >= datetime.time(15, 30) and not getattr(self, "daily_perf_done", False):
            self._save_daily_performance()
            self._generate_performance_report()
            self.daily_perf_done = True

    # -------------------------
    # Exit Logic (NEW HTS STYLE)
    # 손절/구조이탈/분할익절 규칙
    # EOD 전량청산은 별도 함수에서 처리
    # -------------------------
    def _check_exit_strategy(self, code: str, t: TradeItem):

        if t.pos_qty <= 0:
            return

        if getattr(t, "pending_sell", False):
            return

        series, _ = self._series_3m(t)
        if not series or len(series) < 10:
            return

        # 청산 판단용 RSI 계산
        r = rsi_series(series, self.rsi_period)
        if not r or r[-1] is None:
            return

        rsi = float(r[-1])
        close = float(series[-1])

        # 청산 판단용 EMA5/EMA60 계산
        ema5_series = ema_series(series, 5)
        ema60_series = ema_series(series, 60)

        if not ema5_series or not ema60_series:
            return

        ema5_last = ema5_series[-1]
        ema60_last = ema60_series[-1]

        # =========================
        # 청산 조건 평가
        # =========================

        if t.avg_price > 0:
            loss_pct = (close - t.avg_price) / t.avg_price * 100

            # 평단 대비 -4% 손절 실행
            if loss_pct <= -4:
                self.log(f"[STOP LOSS -4%] {code} loss={loss_pct:.2f}% -> EXIT ALL")
                self._sell_market(code, t.pos_qty, reason="STOP -4%")
                return

        # EMA60 하향 이탈 시 전량 청산
        if close < ema60_last:
            self.log(f"[EMA60 BREAK] {code} -> EXIT ALL")
            self._sell_market(code, t.pos_qty, reason="EMA60 BREAK")
            return

        # =========================
        # 청산 조건 평가
        # =========================
        if t.stage >= 1 and not getattr(t, "sell1_done", False):

            if rsi >= 70:
                qty = max(1, int(t.pos_qty * 0.5))

                self.log(f"[SELL1] RSI>=70 -> 50% SELL {code} qty={qty}")
                self._sell_market(code, qty, reason="SELL1 RSI70")

                t.sell1_done = True
                t.signal = "SELL1_RSI70_50%"
                self._update_trade_row(code, signal=t.signal)
                return

        # =========================
        # 청산 조건 평가
        # =========================
        if t.stage >= 1 and getattr(t, "sell1_done", False):

            if rsi >= 70 and close < ema5_last:
                self.log(f"[SELL2] RSI>=70 & close<EMA5 -> EXIT ALL {code}")

                self._sell_market(code, t.pos_qty, reason="SELL2 final exit")

                t.stage = 0
                t.sell1_done = False
                t.signal = "SELL2_EXIT_ALL"

                self._update_trade_row(code, signal=t.signal)
                return
    # -------------------------
    # 장마감 시간대 강제 청산 규칙
    # -------------------------
    def _check_eod_profit_exit_or_hold(self, code: str, t: TradeItem):

        if t.pos_qty <= 0:
            return

        now = datetime.datetime.now()
        hhmm = now.hour * 100 + now.minute

        if hhmm < 1515:
            return

        today = datetime.date.today().isoformat()

        if t.last_eod_date == today:
            return

        t.last_eod_date = today

        self.log(f"[EOD] 15:15 이후 전량 청산 -> SELL ALL {code}")
        self._sell_market(code, t.pos_qty, reason="EOD full exit")

    # -------------------------
    # Orders (with optional simulation)
    # -------------------------
    def _buy_market(self, code: str, qty: int, reason: str = ""):
        est_buy_px = self._apply_slippage(self.trade_map.get(code).last if code in self.trade_map else 0, "BUY")
        self.log(
            f"[BUY] {code} qty={qty} est_px={est_buy_px} arm_live={self.arm_live} "
            f"regime={self.market_regime} reason={reason}"
        )
        t = self.trade_map.get(code)

        # 유효 주문 대상/수량 검증
        if not t or qty <= 0:
            return

        if self.daily_loss_hit:
            self._log_risk_once(
                f"buy_block_loss_{code}",
                f"[BUY-BLOCK][LOSS_LIMIT] {code} daily loss limit already hit",
            )
            return

        # 중복 매수 주문 방지
        if t.pending_buy:
            self.log(f"[BUY-SKIP] {code} pending_buy=True (skip) reason={reason}")
            return

        # 주문 전 HTS 연결 상태 검증
        if self.ocx.GetConnectState() != 1:
            self.log("[ORDER BLOCK] HTS not connected (GetConnectState!=1)")
            return

        # 주문 전 pending_buy 설정
        t.pending_buy = True

        # 실주문/모의주문 시도 건수 집계
        if self.arm_live:
            self.daily_sum.buy_attempts_live += 1
        else:
            self.daily_sum.buy_attempts_sim += 1
        self.daily_sum._bump(code, "BUY_ATTEMPT", 1)

        # -------------------------
        # 매수 주문 가드/전송
        # -------------------------
        if not self.arm_live:
            try:
                if self.SIM_FILL_WHEN_ARM_OFF and t.last > 0:
                    fill_px = self._apply_slippage(t.last, "BUY")
                    prev_qty = t.pos_qty
                    new_qty = prev_qty + qty

                    if new_qty > 0:
                        new_avg = ((t.avg_price * prev_qty) + (fill_px * qty)) / new_qty if prev_qty > 0 else float(
                            fill_px)
                    else:
                        new_avg = 0.0

                    t.pos_qty = new_qty
                    t.avg_price = new_avg
                    self.today_spent += fill_px * qty
                    self.today_total_spent += fill_px * qty
                    t.stage = 1 if t.stage == 0 else min(2, t.stage + 1)
                    t.signal = "HOLD"

                    self._apply_config()
                    self._update_trade_row(code, pos_qty=t.pos_qty, avg_price=t.avg_price, stage=t.stage,
                                           signal=t.signal)
                    self.log(
                        f"  -> SIM FILL BUY: pos_qty={t.pos_qty} avg_price={t.avg_price:.2f} today_spent={self.today_spent:,}")
                else:
                    self.log("  -> ARM LIVE OFF: (mock order only)")
            finally:
                # 모의 주문 종료 후 pending_buy 해제
                t.pending_buy = False
            return

        # -------------------------
        # 매수 주문 가드/전송
        # -------------------------
        acc = self.cmb_account.currentText().strip()
        ret = self.ocx.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            ["BUY", self.screen_order, acc, 1, code, qty, 0, "03", ""]
        )
        self.log(f"  -> SendOrder ret={ret}")

        # 주문 실패 시 pending_buy 복구
        if ret != 0:
            t.pending_buy = False
            self.log(f"[BUY-FAIL] {code} ret={ret} -> pending_buy reset")

    def _sell_market(self, code: str, qty: int, reason: str = ""):
        est_sell_px = self._apply_slippage(self.trade_map.get(code).last if code in self.trade_map else 0, "SELL")
        self.log(f"[SELL] {code} qty={qty} est_px={est_sell_px} arm_live={self.arm_live} reason={reason}")
        t = self.trade_map.get(code)

        # 유효 주문 대상/수량 검증
        if not t or qty <= 0:
            return

        # 중복 매도 주문 방지
        if t.pending_sell:
            self.log(f"[SELL-SKIP] {code} pending_sell=True (skip) reason={reason}")
            return

        # 주문 전 HTS 연결 상태 검증
        if self.ocx.GetConnectState() != 1:
            self.log("[ORDER BLOCK] HTS not connected (GetConnectState!=1)")
            return

        # 주문 전 pending_sell 설정
        t.pending_sell = True

        # 실주문/모의주문 시도 건수 집계
        if self.arm_live:
            self.daily_sum.sell_attempts_live += 1
        else:
            self.daily_sum.sell_attempts_sim += 1
        self.daily_sum._bump(code, "SELL_ATTEMPT", 1)

        # -------------------------
        # 매도 주문 가드/전송
        # -------------------------
        if not self.arm_live:
            try:
                if self.SIM_FILL_WHEN_ARM_OFF:
                    # 보유 중이면 신규 진입 금지
                    if t.pos_qty > 0 and t.avg_price > 0 and t.last > 0:
                        sell_qty = min(qty, t.pos_qty)
                        sim_fill_px = self._apply_slippage(t.last, "SELL")
                        sim_realized = int((sim_fill_px - t.avg_price) * sell_qty)
                        t.realized_pnl_today += sim_realized
                        self.today_total_realized += sim_realized

                    t.pos_qty = max(0, t.pos_qty - qty)
                    if t.pos_qty == 0:
                        t.avg_price = 0.0
                        t.stage = 0
                        t.signal = "WAIT"
                        t.stop1_done = False
                    else:
                        t.signal = "HOLD"

                    self._update_trade_row(code, pos_qty=t.pos_qty, avg_price=t.avg_price, stage=t.stage,
                                            signal=t.signal)
                    self.log(f"  -> SIM FILL SELL: pos_qty={t.pos_qty} avg_price={t.avg_price:.2f}")
                else:
                    self.log("  -> ARM LIVE OFF: (mock order only)")
            finally:
                # 모의 주문 종료 후 pending_sell 해제
                t.pending_sell = False
            return

        # -------------------------
        # 매도 주문 가드/전송
        # -------------------------
        acc = self.cmb_account.currentText().strip()
        ret = self.ocx.dynamicCall(
            "SendOrder(QString, QString, QString, int, QString, int, int, QString, QString)",
            ["SELL", self.screen_order, acc, 2, code, qty, 0, "03", ""]
        )
        self.log(f"  -> SendOrder ret={ret}")

        # 주문 실패 시 pending_sell 복구
        if ret != 0:
            t.pending_sell = False
            self.log(f"[SELL-FAIL] {code} ret={ret} -> pending_sell reset")

    # -------------------------
    # Helpers
    # -------------------------
    def _clear_table(self, tbl: QTableWidget):
        tbl.setRowCount(0)


def main():
    app = QApplication(sys.argv)
    w = AutoTrader()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()

