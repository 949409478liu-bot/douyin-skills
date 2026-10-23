import unittest
from douyin_target_selector import select_target, DouyinTargetError


def page(identifier, url, kind="page"):
    return {"id": identifier, "type": kind, "url": url, "title": identifier}


def responsive(target):
    return dict(url=target['url'], title=target['title'], readyState='complete')


class SelectorTests(unittest.TestCase):
    def test_mixed_pages_do_not_probe_other_sites(self):
        seen = []
        def probe(t):
            seen.append(t['id'])
            return responsive(t)
        selected, _ = select_target([page('map', 'https://nominatim.openstreetmap.org/'),
                                    page('wiki', 'https://commons.wikimedia.org/'),
                                    page('dy', 'https://www.douyin.com/')], probe)
        self.assertEqual(selected['id'], 'dy')
        self.assertEqual(seen, ['dy'])

    def test_ranking(self):
        targets = [page('home', 'https://douyin.com/'), page('video', 'https://www.douyin.com/video/1'),
                   page('search', 'https://www.douyin.com/search/a')]
        self.assertEqual(select_target(targets, responsive)[0]['id'], 'search')

    def test_unresponsive_search_falls_back_to_video(self):
        def probe(t):
            if t['id'] == 'search':
                raise TimeoutError()
            return responsive(t)
        targets = [page('search', 'https://douyin.com/search/a'), page('video', 'https://douyin.com/video/1')]
        self.assertEqual(select_target(targets, probe)[0]['id'], 'video')

    def test_pin_preserves_same_target(self):
        targets = [page('s', 'https://douyin.com/search/a'), page('v', 'https://douyin.com/video/1')]
        self.assertEqual(select_target(targets, responsive, pinned_id='v')[0]['id'], 'v')

    def test_stale_or_non_douyin_pin_fails_closed(self):
        for pin in ['missing', 'map']:
            with self.assertRaisesRegex(DouyinTargetError, 'DOUYIN_TARGET_INVALID'):
                select_target([page('map', 'https://openstreetmap.org/'), page('dy', 'https://douyin.com/')], responsive, pinned_id=pin)

    def test_missing_and_spoofed_domains_fail_closed(self):
        for url in ['https://douyin.com.evil.test/', 'https://evil.test/?douyin.com',
                    'http://douyin.com/', 'about:blank', 'https://notdouyin.com/']:
            with self.assertRaisesRegex(DouyinTargetError, 'DOUYIN_TARGET_MISSING'):
                select_target([page('wrong', url)], responsive)

    def test_iframe_is_not_a_page(self):
        with self.assertRaisesRegex(DouyinTargetError, 'DOUYIN_TARGET_MISSING'):
            select_target([page('frame', 'https://douyin.com/', 'iframe')], responsive)

    def test_live_navigation_outside_domain_rejected(self):
        with self.assertRaisesRegex(DouyinTargetError, 'DOUYIN_TARGET_INVALID'):
            select_target([page('dy', 'https://douyin.com/')], lambda t: dict(url='https://wikimedia.org/', readyState='complete'))

    def test_unresponsive_pin_does_not_switch_target(self):
        def probe(t):
            raise TimeoutError()
        with self.assertRaisesRegex(DouyinTargetError, 'DOUYIN_TARGET_UNRESPONSIVE'):
            select_target([page('dy', 'https://douyin.com/')], probe, pinned_id='dy')


if __name__ == '__main__':
    unittest.main()
