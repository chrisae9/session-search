"""Controlled foreground search deadlines, independent of background indexing."""


class SearchDeadline(TimeoutError):
    reason = 'search_deadline'
