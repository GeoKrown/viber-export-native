"""Command-line entry point."""
import sys


def main():
    if sys.argv[1:] in (['--help'], ['-h']):
        print('Viber Export Native\n\n'
              'python -m viber_native chats\n'
              'python -m viber_native configure --group "Exact group name"\n'
              'python -m viber_native run [--once]\n'
              'python -m viber_native history [--days 14] [--output FILE]\n'
              'python -m viber_native download --chat-id ID --event-id ID\n\n'
              'Run COMMAND --help for command options. For chats, configure and run,\n'
              'place --config FILE and --db FILE before the command. For history\n'
              'and download, place --config FILE after the command.')
        return 0
    if len(sys.argv) > 1 and sys.argv[1] in ('history', 'download'):
        command = sys.argv[1]
        if command == 'history':
            from .history import main as run
        else:
            from .media import main as run
        return run(sys.argv[2:])
    from .collector import main as run
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
