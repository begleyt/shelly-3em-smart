from .event_detector import StepEventDetector


class AppState:
    def __init__(self) -> None:
        self.detector = StepEventDetector()
        self.last_sample: dict = {}
        self.last_persist_ts: float = 0.0
        self.last_prune_ts: float = 0.0
        self.first_sample_logged: bool = False


state = AppState()
