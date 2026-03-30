import sys
from PyQt5.QtWidgets import QApplication
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer


class Kiwoom:
    def __init__(self):
        self.ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")

        # 로드 확인 (정상이면 보통 문자열이 나옵니다)
        try:
            prog = self.ocx.control()
        except Exception:
            prog = None
        print("Loaded control:", prog)

        # 이벤트 연결 (여기서 에러가 나면 OCX 등록 문제)
        self.ocx.OnEventConnect.connect(self._on_event_connect)
        self.ocx.OnReceiveTrData.connect(self._on_receive_tr_data)

        self.login_loop = QEventLoop()
        self.tr_loop = QEventLoop()
        self._login_ok = False

    def login(self, timeout_ms=30000):
        QTimer.singleShot(timeout_ms, self._login_timeout)
        self.ocx.dynamicCall("CommConnect()")
        self.login_loop.exec_()
        return self._login_ok

    def _login_timeout(self):
        if self.login_loop.isRunning():
            print("로그인 타임아웃(30초).")
            self.login_loop.exit()

    def _on_event_connect(self, err_code):
        self._login_ok = (err_code == 0)
        print("로그인 결과:", "성공" if err_code == 0 else f"실패({err_code})")
        self.login_loop.exit()

    def request_price(self, code="005930", timeout_ms=10000):
        state = self.ocx.dynamicCall("GetConnectState()")
        print("ConnectState:", state)
        if state != 1:
            print("접속 상태가 1이 아닙니다.")
            return

        QTimer.singleShot(timeout_ms, self._tr_timeout)
        self.ocx.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        self.ocx.dynamicCall("CommRqData(QString, QString, int, QString)", "기본정보", "opt10001", 0, "0101")
        self.tr_loop.exec_()

    def _tr_timeout(self):
        if self.tr_loop.isRunning():
            print("TR 타임아웃(10초).")
            self.tr_loop.exit()

    def _on_receive_tr_data(self, scr_no, rq_name, tr_code, record_name, prev_next, data_len, err_code, msg1, msg2):
        if rq_name == "기본정보":
            price = self.ocx.dynamicCall(
                "GetCommData(QString, QString, int, QString)",
                tr_code, rq_name, 0, "현재가"
            ).strip()
            print("현재가:", price)
            self.tr_loop.exit()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    kiwoom = Kiwoom()
    if kiwoom.login():
        kiwoom.request_price("005930")
    sys.exit(0)
