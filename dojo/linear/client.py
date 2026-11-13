# LINEAR-FORK: minimal Linear GraphQL client (https://api.linear.app/graphql).
import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

LINEAR_API_URL = "https://api.linear.app/graphql"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LinearError(Exception):

    """A Linear API call failed. ``retryable`` is False for 4xx-style errors."""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class LinearClient:
    def __init__(self, api_key, auth_type="api_key", timeout=None, max_retries=None):
        self.api_key = api_key
        self.auth_type = auth_type
        self.timeout = timeout or getattr(settings, "LINEAR_TIMEOUT", 10)
        self.max_retries = max_retries if max_retries is not None else getattr(settings, "LINEAR_MAX_RETRIES", 3)

    @classmethod
    def for_instance(cls, linear_instance):
        from dojo.utils import prepare_for_view  # noqa: PLC0415 -- lazy import, avoids circular dependency
        return cls(prepare_for_view(linear_instance.api_key), linear_instance.auth_type)

    def _headers(self):
        # Personal API keys go in the header as-is; OAuth2 tokens use Bearer.
        auth = self.api_key if self.auth_type == "api_key" else f"Bearer {self.api_key}"
        return {"Authorization": auth, "Content-Type": "application/json"}

    def execute(self, query, variables=None):
        attempt = 0
        while True:
            attempt += 1
            try:
                response = requests.post(
                    LINEAR_API_URL,
                    json={"query": query, "variables": variables or {}},
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                error = LinearError(f"Linear request failed: {e}", retryable=True)
            else:
                if response.status_code in RETRYABLE_STATUS:
                    error = LinearError(f"Linear returned HTTP {response.status_code}", retryable=True)
                elif response.status_code >= 400:
                    msg = f"Linear returned HTTP {response.status_code}: {response.text[:300]}"
                    raise LinearError(msg)
                else:
                    payload = response.json()
                    if payload.get("errors"):
                        messages = "; ".join(e.get("message", "") for e in payload["errors"])
                        msg = f"Linear API error: {messages}"
                        raise LinearError(msg)
                    return payload.get("data") or {}
            if attempt > self.max_retries:
                raise error
            time.sleep(min(2 ** (attempt - 1), 8))

    # --- queries -------------------------------------------------------------

    def viewer(self):
        data = self.execute("query { viewer { id name } organization { name } }")
        return {"user": data.get("viewer", {}).get("name"), "workspace": data.get("organization", {}).get("name")}

    def teams(self):
        data = self.execute("query { teams(first: 250) { nodes { id key name } } }")
        return data["teams"]["nodes"]

    def workflow_states(self, team_id):
        data = self.execute(
            "query($teamId: ID!) { team(id: $teamId) { states { nodes { id name type } } } }",
            {"teamId": team_id},
        )
        return data["team"]["states"]["nodes"]

    def labels(self, team_id):
        data = self.execute(
            "query($teamId: ID!) { team(id: $teamId) { labels(first: 250) { nodes { id name } } } }",
            {"teamId": team_id},
        )
        return data["team"]["labels"]["nodes"]

    def get_issue(self, id_or_identifier):
        # Linear's issue(id:) accepts the UUID or the human identifier (e.g. SEC-42).
        data = self.execute(
            "query($id: String!) { issue(id: $id) { id identifier url description team { id } state { id type } } }",
            {"id": id_or_identifier},
        )
        return data.get("issue")

    # --- mutations -----------------------------------------------------------

    def create_issue(self, issue_input):
        data = self.execute(
            "mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier url } } }",
            {"input": issue_input},
        )
        result = data["issueCreate"]
        if not result.get("success"):
            msg = "Linear issueCreate was not successful"
            raise LinearError(msg)
        return result["issue"]

    def update_issue(self, issue_id, issue_input):
        data = self.execute(
            "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success } }",
            {"id": issue_id, "input": issue_input},
        )
        if not data["issueUpdate"].get("success"):
            msg = "Linear issueUpdate was not successful"
            raise LinearError(msg)

    def create_comment(self, issue_id, body):
        data = self.execute(
            "mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }",
            {"input": {"issueId": issue_id, "body": body}},
        )
        return data["commentCreate"].get("success", False)

    def create_attachment(self, issue_id, url, title, subtitle=""):
        data = self.execute(
            "mutation($input: AttachmentCreateInput!) { attachmentCreate(input: $input) { success attachment { id } } }",
            {"input": {"issueId": issue_id, "url": url, "title": title, "subtitle": subtitle}},
        )
        return (data["attachmentCreate"].get("attachment") or {}).get("id", "")
