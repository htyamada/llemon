from django.core.management.base import BaseCommand

from documentview import active


class Command(BaseCommand):
    help = (
        'Report invalid documentview export symlinks (missing source, outside the '
        'collection root, not a regular file, unreadable, or unsupported suffix). '
        'With --repair, delete them. A stray non-symlink entry in the exports '
        'directory is always reported only, as informational only, and never touched.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--repair', action='store_true',
            help='Delete invalid export links instead of only reporting them.',
        )

    def handle(self, *args, **options):
        issues = active.reconcile(repair=options['repair'])
        if not issues:
            self.stdout.write(self.style.SUCCESS('No invalid export links found.'))
            return
        for issue in issues:
            marker = 'REPAIRED' if issue.repaired else issue.kind.upper()
            self.stdout.write(f'[{marker}] {issue.link_name}: {issue.detail}')
