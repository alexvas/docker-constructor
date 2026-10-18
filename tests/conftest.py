"""Apply the permanent skip-review policy to every maintained test run."""

pytest_plugins = ("tests.green_skip_policy",)
