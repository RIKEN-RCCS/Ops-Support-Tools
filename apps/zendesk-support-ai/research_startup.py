"""Start research workers without queue/config/Zendesk startup privileges."""
import os
import sys
import common


def main():
    if len(sys.argv) < 2:
        raise SystemExit('worker command required')
    if not common.knowledge_enabled():
        raise SystemExit('Knowledge endpoint required')
    if not common.env_secret('SUPPORT_AI_KNOWLEDGE_TOKEN'):
        raise SystemExit('Knowledge client credential required')
    common.knowledge_list_runs(limit=1)
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == '__main__':
    main()
