"""A capture still being taken, for the viewer's live view: packets added as they come (add()), then decoded into the
view looked at (decode()), each checked against the display filter on the way. What the summaries and problems say
is worked out from what's come so far (finish())."""
from . import capfile, dfilter

DEFAULT_UNWRAP_AFTER = 200  # Packets seen before deciding whether to show the frames mirrored over ERSPAN


class LiveCapture:
    def __init__(self, name, keep_duplicates=False, unwrapped=None):
        """unwrapped: show the frames mirrored over ERSPAN (None: decide once a few packets have come, as a file
        opened would)."""
        self.capture = capfile.CaptureFile(name)
        self.intake = capfile.Intake(self.capture, keep_duplicates)
        self.decided = unwrapped is not None
        self.analyzer = self.analyzer_for(bool(unwrapped))
        self.filter = None
        self.full = False  # Holds all it can (capfile.MAX_PACKETS): no more are added

    def analyzer_for(self, unwrapped):
        capture = self.capture
        return capfile.Analyzer(capture, capture.mirrored if unwrapped else capture.captured, unwrapped)

    @property
    def view(self):
        return self.analyzer.view

    @property
    def unwrapped(self):
        return self.view.unwrapped

    @property
    def waiting(self):
        """Packets added but not yet decoded."""
        return self.analyzer.waiting

    def add(self, records):
        """Take packets (reader.Records, in time order). True if the view switched to the mirrored frames (start
        again from show_rows: nothing was decoded yet)."""
        if not self.full:
            intake = self.intake
            for record in records:
                if not intake.add(record):
                    self.full = True
                    break
        if not self.decided and len(self.capture.captured) >= DEFAULT_UNWRAP_AFTER:
            self.decided = True
            if self.capture.default_unwrapped():
                self.analyzer = self.analyzer_for(True)
                return True
        return False

    def decode(self, limit=None):
        """Decode up to limit of the packets waiting: the indexes (in the view's packets) of those the filter
        shows."""
        analyzer, filter_ = self.analyzer, self.filter
        shown = []
        count = 0
        while analyzer.decoded < len(analyzer.view.packets) and (limit is None or count < limit):
            index = analyzer.decoded
            d = analyzer.add()
            count += 1
            if filter_ is None:
                shown.append(index)
                continue
            packet = analyzer.view.packets[index]
            try:
                if filter_.test(None if filter_.frame_only else d, packet):
                    shown.append(index)
            except Exception:  # As dfilter.apply: a packet a filter trips over isn't shown
                pass
        return shown

    def show(self, unwrapped):
        """Look at the mirrored frames (unwrapped) or the packets as captured: decoded again from the start."""
        self.decided = True
        if bool(unwrapped) != self.unwrapped:
            self.analyzer = self.analyzer_for(bool(unwrapped))

    def set_filter(self, filter_, should_stop=None):
        """Filter with filter_ (None: show all) from now on: the indexes of the decoded packets it shows (None if
        stopped)."""
        self.filter = filter_
        return dfilter.apply(filter_, self.view.packets[:self.analyzer.decoded], should_stop)

    def shown_so_far(self):
        """The indexes of the decoded packets the filter shows."""
        return self.set_filter(self.filter)

    def finish(self):
        """The view with its summaries and problems found, from what's been decoded."""
        return self.analyzer.finish()
