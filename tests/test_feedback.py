import os
import re
import unittest

from asf import env

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(PROJECT_ROOT, '.github', 'ISSUE_TEMPLATE')

FORMS = {
    'bug.yml': 'bug',
    'idea.yml': 'idea',
    'install.yml': 'install',
}

_FORBIDDEN = re.compile(r'\b(name|account|path)\b', re.IGNORECASE)


def _load(filename):
    with open(os.path.join(TEMPLATE_DIR, filename), encoding='utf-8') as f:
        return env.loads(f.read())


class TemplateTests(unittest.TestCase):
    def test_files_parse(self):
        for filename in list(FORMS) + ['config.yml']:
            with self.subTest(filename=filename):
                self.assertIsInstance(_load(filename), dict)

    def test_each_form_carries_feedback_and_its_own_label(self):
        for filename, label in FORMS.items():
            with self.subTest(filename=filename):
                self.assertEqual(_load(filename).get('labels'), ['feedback', label])

    def test_blank_issues_are_off(self):
        self.assertIs(_load('config.yml').get('blank_issues_enabled'), False)

    def test_no_field_asks_for_a_name_account_or_path(self):
        for filename in FORMS:
            data = _load(filename)
            for item in data.get('body', []):
                attrs = item.get('attributes') or {}
                for key in ('label', 'description', 'placeholder'):
                    value = attrs.get(key)
                    if not value:
                        continue
                    self.assertNotRegex(
                        value, _FORBIDDEN,
                        f"{filename} field {item.get('id')!r} {key} asks for a name, an "
                        f"account or a path: {value!r}")


if __name__ == '__main__':
    unittest.main()
