# LINEAR-FORK: REST endpoints for the Linear buttons (same rules as the UI).
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from dojo.authorization.authorization import user_has_permission
from dojo.linear import services
from dojo.linear.client import LinearError
from dojo.models import Finding


class LinearPushSerializer(serializers.Serializer):
    finding_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)
    title = serializers.CharField(required=False, allow_blank=True)


class LinearLinkSerializer(serializers.Serializer):
    finding_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)
    identifier = serializers.CharField()


class _LinearActionView(APIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = None

    def _findings(self, request, ids):
        findings = list(Finding.objects.filter(id__in=ids).select_related("test__engagement__product"))
        if len(findings) != len(set(ids)) or not all(user_has_permission(request.user, f, "edit") for f in findings):
            return None
        return findings

    def post(self, request):
        if not services.is_enabled():
            return Response({"error": "The Linear integration is disabled."}, status=status.HTTP_400_BAD_REQUEST)
        ser = self.serializer_class(data=request.data)
        ser.is_valid(raise_exception=True)
        findings = self._findings(request, ser.validated_data["finding_ids"])
        if findings is None:
            return Response({"error": "Unknown finding or no edit permission."}, status=status.HTTP_403_FORBIDDEN)
        try:
            issue, skipped = self.act(request, findings, ser.validated_data)
        except LinearError as e:
            return Response({"error": str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({
            "issue": {"identifier": issue.identifier, "url": issue.url} if issue else None,
            "skipped": [{"finding_id": f.id, "reason": reason} for f, reason in skipped],
        }, status=status.HTTP_200_OK if issue else status.HTTP_400_BAD_REQUEST)


class LinearPushView(_LinearActionView):

    """Create one Linear issue for the given findings (one finding = the "Push to Linear" button)."""

    serializer_class = LinearPushSerializer

    def act(self, request, findings, data):
        return services.create_issue(findings, user=request.user, title=data.get("title") or None)


class LinearLinkView(_LinearActionView):

    """Link the given findings to an existing Linear issue (e.g. SEC-42)."""

    serializer_class = LinearLinkSerializer

    def act(self, request, findings, data):
        return services.link_to_existing(findings, data["identifier"], user=request.user)
