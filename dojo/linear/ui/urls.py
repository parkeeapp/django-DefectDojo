from django.urls import re_path

from dojo.linear.ui import views

urlpatterns = [
    re_path(r"^linear$", views.linear_instances, name="linear"),
    re_path(r"^linear/add$", views.edit_linear_instance, name="add_linear"),
    re_path(r"^linear/(?P<lid>\d+)/edit$", views.edit_linear_instance, name="edit_linear"),
    re_path(r"^linear/(?P<lid>\d+)/delete$", views.delete_linear_instance, name="delete_linear"),
    re_path(r"^linear/bulk$", views.bulk_linear_action, name="linear_bulk_action"),
    re_path(r"^product/(?P<pid>\d+)/linear$", views.product_linear_config, name="product_linear_config"),
    re_path(r"^finding/(?P<fid>\d+)/linear/(?P<action>push|link|repush|unlink)$", views.finding_linear_action, name="finding_linear_action"),
]
