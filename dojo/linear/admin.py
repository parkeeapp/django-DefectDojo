from django.contrib import admin

from dojo.linear.models import (
    Linear_Baseline,
    Linear_Finding_Link,
    Linear_Instance,
    Linear_Issue,
    Linear_Product_Config,
)


@admin.register(Linear_Instance)
class Linear_Instance_Admin(admin.ModelAdmin):
    # Never show the (encrypted) key in admin.
    exclude = ("api_key",)
    list_display = ("name", "auth_type", "workspace_name")


admin.site.register(Linear_Product_Config)
admin.site.register(Linear_Issue)
admin.site.register(Linear_Finding_Link)
admin.site.register(Linear_Baseline)
