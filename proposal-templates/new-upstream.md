{% extends "base.md" %}
{% block runner %}
{% if role == 'pristine-tar' %}
pristine-tar data for new upstream version {{ upstream_version }}.
{% elif role == 'upstream' %}
Import of new upstream version {{ upstream_version }}.
{% elif role == 'main' %}
Merge new upstream version {{ upstream_version }}.
{% endif %}
{% endblock %}