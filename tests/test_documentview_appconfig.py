import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'lib'
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from documentview import appconfig


class DocumentViewAppConfigTests(unittest.TestCase):
    def test_init_variant_loads_documentview_conf(self) -> None:
        fake_appconfig = object()
        with mock.patch.object(appconfig, 'AppConfig', return_value=fake_appconfig) as appconfig_cls:
            with mock.patch.object(appconfig, 'init') as init_mock:
                appconfig.init_variant('hty7')

        expected_conf = ROOT / 'etc' / 'documentview.conf'
        appconfig_cls.assert_called_once_with(str(expected_conf), 'hty7')
        init_mock.assert_called_once_with(fake_appconfig)

    def test_init_strips_whitespace_from_values(self) -> None:
        ac = mock.Mock()
        ac.get.side_effect = lambda _project, _layer, key: {
            'root': '  /srv/books  ',
            'active_dir': '  ~/var/documentview/reader  ',
        }[key]

        appconfig.init(ac)

        self.assertEqual(appconfig.root, '/srv/books')
        self.assertEqual(appconfig.active_dir, '~/var/documentview/reader')

    def test_init_defaults_missing_values_to_empty_string(self) -> None:
        ac = mock.Mock()
        ac.get.return_value = None

        appconfig.init(ac)

        self.assertEqual(appconfig.root, '')
        self.assertEqual(appconfig.active_dir, '')


if __name__ == '__main__':
    unittest.main()
