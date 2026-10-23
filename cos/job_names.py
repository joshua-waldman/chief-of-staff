"""Compact list labels; full source requests stay in job details."""
import re


NAMING_POLICY = """Include job_name when calling update_brief: a clear 3-7 word
name for the job's main topic or outcome (for example, 'Release readiness review'
or 'Quarterly budget approval'). Use a short noun phrase, not a detailed request,
status report or list of steps. Never include message, chat, conversation or
meeting IDs, URLs, timestamps, or source metadata. Keep it under 60 characters.
"""


def compact_name(value):
    value = re.sub(r'https?://\S+|\b\S+@\S+\b', '', value or '', flags=re.I)
    value = re.sub(r'\b(?:teams\s+)?(?:message|meeting|chat|conversation|thread|email)[ _-]*(?:id\b\s*[:=#-]?|#)\s*\S+', '', value, flags=re.I)
    value = re.sub(r'\b[0-9a-f]{8}-[0-9a-f-]{27,}\b|\b\d{10,}\b|\b\S{32,}\b', '', value, flags=re.I)
    value = re.sub(r'^(?:(?:re|fw|fwd)\s*:\s*)+', '', value.strip(), flags=re.I)
    value = re.sub(r'^(?:please\s+|can you\s+|could you\s+)', '', value, flags=re.I)
    value = re.split(r'[\n.!?;]|\s+[—–]\s+', value)[0]
    value = ' '.join(value.split()[:7]).strip(' :,-#')
    if len(value) > 60:
        value = value[:61].rsplit(' ', 1)[0].rstrip(' :,-#')
    return value


def display_name(job, brief):
    for candidate in (brief.get('job_name'), brief.get('email_subject'), brief.get('context'), job.get('title')):
        name = compact_name(candidate)
        if not name or re.match(r'^(?:Teams conversation|Triage this incoming|Message ID|Chat ID)\b', name, re.I):
            continue
        return name
    return 'Conversation review' if job.get('org') == 'Communications' else 'Task review'
