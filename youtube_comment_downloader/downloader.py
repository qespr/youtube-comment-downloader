#!/usr/bin/env python

from __future__ import print_function

import argparse
import io
import json
import os
import sys
import time

import requests

YOUTUBE_VIDEO_URL = 'https://www.youtube.com/watch?v={youtube_id}'
YOUTUBE_COMMENTS_AJAX_URL = 'https://www.youtube.com/comment_service_ajax'

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.130 Safari/537.36'

SORT_BY_POPULAR = 0
SORT_BY_RECENT = 1


def find_value(html, key, num_chars=2, separator='"'):
    pos_begin = html.find(key) + len(key) + num_chars
    pos_end = html.find(separator, pos_begin)
    return html[pos_begin: pos_end]


def ajax_request(session, url, params=None, data=None, headers=None, retries=5, sleep=20):
    for _ in range(retries):
        response = session.post(url, params=params, data=data, headers=headers)
        if response.status_code == 200:
            return response.json()
        if response.status_code > 399:
            print("Error: Http request returned bad status code: " + response.status_code + ", " + _ + " times.")
            return {}
        else:
            time.sleep(sleep)


def download_comments(youtube_id, sort_by=SORT_BY_RECENT, sleep=.1):
    session = requests.Session()
    session.headers['User-Agent'] = USER_AGENT

    response = session.get(YOUTUBE_VIDEO_URL.format(youtube_id=youtube_id))

    if 'uxe=' in response.request.url:
        session.cookies.set('CONSENT', 'YES+cb', domain='.youtube.com')
        response = session.get(YOUTUBE_VIDEO_URL.format(youtube_id=youtube_id))

    html = response.text
    session_token = find_value(html, 'XSRF_TOKEN', 3)
    session_token = session_token.encode('ascii').decode('unicode-escape')

    data = json.loads(find_value(html, 'var ytInitialData = ', 0, '};') + '}')
    for renderer in search_dict(data, 'itemSectionRenderer'):
        ncd = next(search_dict(renderer, 'nextContinuationData'), None)
        if ncd:
            break

    try:
        ncd
    except NameError:
        print("Comments disabled or video does not exist")
        return

    needs_sorting = sort_by != SORT_BY_POPULAR
    continuations = [(ncd['continuation'], ncd['clickTrackingParams'], 'action_get_comments')]
    while continuations:
        continuation, itct, action = continuations.pop()
        response = ajax_request(session, YOUTUBE_COMMENTS_AJAX_URL,
                                params={action: 1,
                                        'pbj': 1,
                                        'ctoken': continuation,
                                        'continuation': continuation,
                                        'itct': itct},
                                data={'session_token': session_token},
                                headers={'X-YouTube-Client-Name': '1',
                                         'X-YouTube-Client-Version': '2.20201202.06.01'})

        if not response:
            break
        if list(search_dict(response, 'externalErrorMessage')):
            raise RuntimeError('Error returned from server: ' + next(search_dict(response, 'externalErrorMessage')))

        if needs_sorting:
            sort_menu = next(search_dict(response, 'sortFilterSubMenuRenderer'), {}).get('subMenuItems', [])
            if sort_by < len(sort_menu):
                ncd = sort_menu[sort_by]['continuation']['reloadContinuationData']
                continuations = [(ncd['continuation'], ncd['clickTrackingParams'], 'action_get_comments')]
                needs_sorting = False
                continue
            raise RuntimeError('Failed to set sorting')

        if action == 'action_get_comments':
            section = next(search_dict(response, 'itemSectionContinuation'), {})
            for continuation in section.get('continuations', []):
                ncd = continuation['nextContinuationData']
                continuations.append((ncd['continuation'], ncd['clickTrackingParams'], 'action_get_comments'))
            for item in section.get('contents', []):
                continuations.extend([(ncd['continuation'], ncd['clickTrackingParams'], 'action_get_comment_replies')
                                      for ncd in search_dict(item, 'nextContinuationData')])

        elif action == 'action_get_comment_replies':
            continuations.extend([(ncd['continuation'], ncd['clickTrackingParams'], 'action_get_comment_replies')
                                  for ncd in search_dict(response, 'nextContinuationData')])

        for comment in search_dict(response, 'commentRenderer'):
            yield {'cid': comment['commentId'],
                   'text': ''.join([c['text'] for c in comment['contentText'].get('runs', [])]),
                   'time': comment['publishedTimeText']['runs'][0]['text'],
                   'author': comment.get('authorText', {}).get('simpleText', ''),
                   'channel': comment['authorEndpoint']['browseEndpoint'].get('browseId', ''),
                   'votes': comment.get('voteCount', {}).get('simpleText', '0'),
                   'photo': comment['authorThumbnail']['thumbnails'][-1]['url'],
                   'heart': next(search_dict(comment, 'isHearted'), False)}

        time.sleep(sleep)


def search_dict(partial, search_key):
    stack = [partial]
    while stack:
        current_item = stack.pop()
        if isinstance(current_item, dict):
            for key, value in current_item.items():
                if key == search_key:
                    yield value
                else:
                    stack.append(value)
        elif isinstance(current_item, list):
            for value in current_item:
                stack.append(value)


def prepareDownload(ytid, outputFile, sort, limit):
    print('Downloading Youtube comments for video:', ytid)
    count = 0
    with io.open(outputFile, 'w', encoding='utf8') as fp:
        sys.stdout.write('Downloaded %d comment(s)\r' % count)
        sys.stdout.flush()
        start_time = time.time()
        for comment in download_comments(ytid, sort):
            comment_json = json.dumps(comment, ensure_ascii=False)
            print(comment_json.decode('utf-8') if isinstance(comment_json, bytes) else comment_json, file=fp)
            count += 1
            sys.stdout.write('Downloaded %d comment(s)\r' % count)
            sys.stdout.flush()
            if limit and count >= limit:
                break
    print('\n[{:.2f} seconds] Done!'.format(time.time() - start_time))


# Extracts id from (hopefully) all YouTube urls
def extractID(source):
    if "/youtu.be/" in source:
        return source[source.rfind("/")+1: source.find("?") if source.find("?") != -1 else None]
    if "youtube" in source:
        return source[source.find("v=")+2: source.find("&") if source.find("&") != -1 else None]
    return source


def main(argv = None):
    parser = argparse.ArgumentParser(add_help=False, description=('Download Youtube comments without using the Youtube API'))
    parser.add_argument('--help', '-h', action='help', default=argparse.SUPPRESS, help='Show this help message and exit')
    parser.add_argument('--youtubeid', '-y', help='ID or URL of Youtube video for which to download the comments')
    parser.add_argument('--output', '-o', help='Output filename (output format is line delimited JSON)')
    parser.add_argument('--limit', '-l', type=int, help='Limit the number of comments')
    parser.add_argument('--sort', '-s', type=int, default=SORT_BY_RECENT,
                        help='Whether to download popular (0) or recent comments (1). Defaults to 1')

    try:
        args = parser.parse_args() if argv is None else parser.parse_args(argv)

        youtube_id = args.youtubeid
        output = args.output
        limit = args.limit

        if not youtube_id:
            parser.print_usage()
            raise ValueError('You need to specify a Youtube ID or URL')

        if not output:
            print("No output file specified, saving to [youtubeid].json")
            output = extractID(youtube_id) + ".json"

        if os.sep in output:
            outdir = os.path.dirname(output)
            if not os.path.exists(outdir):
                os.makedirs(outdir)

        prepareDownload(extractID(youtube_id), output, args.sort, limit)
    except Exception as e:
        print('Error:', str(e))
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv[1:])
