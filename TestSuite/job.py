from pathlib import Path
from typing import List, Optional

from TestSuite.json_based import JSONBased


class Job(JSONBased):
    def __init__(self, pure_name: str, jobs_dir_path: Path, is_feed: bool, selected_feeds: Optional[List[str]] = None):
        super().__init__(jobs_dir_path, pure_name, "job")
        self.pure_name = pure_name
        self.is_feed = is_feed
        self.selected_feeds = selected_feeds

        self.create_default_job()

    def create_default_job(self):
        self.write_json({
            'fromServerVersion': '6.5.0',
            'id': self.pure_name,  # todo
            'name': self.pure_name,
            'isFeed': self.is_feed,
            'selectedFeeds': self.selected_feeds or [],
            'isAllFeeds': self.is_feed and not self.selected_feeds,

            'minutesToTimeout': 0,  # todo
            'description': "",
            'playbookId': "",  # todo
            'currentIncidentId': 1,
            'lastRunTime': '',  # todo
            'nextRunTime': '',  # todo
            'displayNextRunTime': '',  # todo
            'disabledNextRunTime': '',  # todo
            'schedulingStatus': 'enabled',
            'previousRunStatus': 'idle',  # todo
            'tags': [],
            'shouldTriggerNew': False,
            'closePrevRun': False,
            'notifyOwner': False,
        })
