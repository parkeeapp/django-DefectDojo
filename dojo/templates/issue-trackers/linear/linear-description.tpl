{% load display_tags %}{% autoescape off %}{% comment %}
LINEAR-FORK: Markdown body of a Linear issue. One block per finding. The DefectDojo
reference section is appended at the bottom by dojo/linear/services.py, not here.
{% endcomment %}{% for finding in findings %}{% if findings|length > 1 %}### {{ finding.title }}

{% endif %}**Severity:** {{ finding.severity }}{% if finding.cvssv3_score %} (CVSS {{ finding.cvssv3_score }}){% endif %}
{% if finding.cwe and finding.cwe > 0 %}**CWE:** [CWE-{{ finding.cwe }}]({{ finding.cwe|cwe_url }})
{% endif %}{% if finding.vulnerability_ids %}**Vulnerability IDs:** {{ finding.vulnerability_ids|join:", " }}
{% endif %}**Location:** {{ finding.test.engagement.product.name }} > {{ finding.test.engagement.name }} > {{ finding.test }}
{% if finding.file_path %}**File:** `{{ finding.file_path }}{% if finding.line %}:{{ finding.line }}{% endif %}`
{% endif %}{% if finding.component_name %}**Component:** {{ finding.component_name }}{% if finding.component_version %} {{ finding.component_version }}{% endif %}
{% endif %}
#### Description

{{ finding.description }}
{% if finding.impact %}
#### Impact

{{ finding.impact }}
{% endif %}{% if finding.mitigation %}
#### Mitigation

{{ finding.mitigation }}
{% endif %}{% if not forloop.last %}

{% endif %}{% endfor %}{% endautoescape %}
