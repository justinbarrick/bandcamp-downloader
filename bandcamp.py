#!/usr/bin/env python3
import json
import requests
import os
import time
import urllib.parse
import sys
import zipfile
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from html.parser import HTMLParser

class BandcampBlobParser(HTMLParser):
    def __init__(self, session=None):
        self.session = session or requests
        self.__datablob = None
        super().__init__()

    @property
    def data(self):
        return self.__datablob or {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        elemclass = attrs.get('id')

        if tag != 'div' or elemclass != 'pagedata':
            return

        self.__datablob = json.loads(attrs.get('data-blob'))

class ParseUserInfo(BandcampBlobParser):
    @property
    def fan_id(self):
        return int(self.data.get('fan_data', {}).get('fan_id', 0))

    @property
    def last_token(self):
        return self.data.get('collection_data', {}).get('last_token', '')

    @property
    def albums(self):
        collection = self.data.get('item_cache', {}).get('collection', {})
        download_urls = self.data.get('collection_data', {}).get('redownload_urls', {})
        return map_download_urls(download_urls, list(collection.values()))

    def get_albums(self, username):
        response = self.session.get('https://bandcamp.com/' + username)
        self.feed(response.text)
        return self.data

class DownloadAlbum(BandcampBlobParser):
    def __init__(self, album, session=None, base_dir=None):
        self.album = album
        self.base_dir = base_dir or "Music"
        super().__init__(session=session)
        self.__download_url = None

    @property
    def download_url(self):
        return self.__download_url

    def parse_album(self):
        response = self.session.get(self.album['download_url'])
        self.feed(response.text)

        for download in self.data['digital_items']:
            self.__download_url = download['downloads']['mp3-v0']['url']

        return self.download_url

    @property
    def download_dir(self):
        download_dir = os.path.join(self.base_dir, self.album['band_name'], self.album['item_title'])

        if not os.path.exists(download_dir):
            os.makedirs(download_dir)

        return download_dir

    @property
    def download_path(self):
        return os.path.join(self.download_dir, str(self.album['item_id']) + '.zip')

    @property
    def lock_path(self):
        return os.path.join(self.download_dir, '.{}.lock'.format(str(self.album['item_id'])))

    @property
    def locked(self):
        return os.path.exists(self.lock_path)

    def lock(self):
        with open(self.lock_path, 'w'):
            pass

    def extract(self):
        try:
            with zipfile.ZipFile(self.download_path, 'r') as zipped:
                zipped.extractall(self.download_dir)
        except zipfile.BadZipFile:
            os.rename(self.download_path, os.path.join(self.download_dir, self.album['item_title'] + ".mp3"))

    def download(self):
        with open(self.download_path, 'wb') as download:
            response = self.session.get(self.download_url, stream=True)
            for block in response.iter_content(1024):
                download.write(block)

    def fetch_album(self):
        album_name = '{} - {}'.format(self.album['band_name'], self.album['item_title'])

        if self.locked:
            print('Skipping already fetched album, {}'.format(album_name))
            return

        print('Fetching album, {}'.format(album_name))
        self.download()
        self.extract()

        self.lock()

def map_download_urls(download_urls, albums):
    for album in albums:
        url_key = album['sale_item_type'] + str(album['sale_item_id'])
        album['download_url'] = download_urls.get(url_key)
    return albums

def get_collection(fan_id, last_token, albums=None, session=None):
    collection_items = 'https://bandcamp.com/api/fancollection/1/collection_items'

    if not session:
        session = requests

    response = session.post(collection_items, data=json.dumps({
        'fan_id': fan_id,
        'older_than_token': last_token,
        'count': 100
    }))

    if not albums:
        albums = []

    response_json = response.json()
    items = response_json.get('items', [])
    items = map_download_urls(response_json['redownload_urls'], items)

    if not items:
        return albums

    return get_collection(fan_id, items[-1]['token'], albums=albums + items, session=session)

def wait_for_id(driver, element_id):
    WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, element_id)))

def parse_cookie_list(cookies):
    cookie_jar = requests.cookies.RequestsCookieJar()

    now = time.time()

    for cookie in cookies:
        if cookie['expiry'] and cookie['expiry'] < now:
            continue

        cookie_jar.set(cookie['name'], cookie['value'], 
                       domain=cookie['domain'], path=cookie['path'], expires=cookie['expiry'])

    return cookie_jar

def bandcamp_login(username, password):
    if os.path.exists('.cookies'):
        cookie_jar = parse_cookie_list(json.load(open('.cookies')))

        if cookie_jar.get('client_id') and cookie_jar.get('identity'):
            return cookie_jar

    driver = webdriver.Firefox()

    cookies = []

    try:
        driver.get('https://bandcamp.com/login')

        wait_for_id(driver, 'username-field')

        uname = driver.find_element_by_id('username-field')
        uname.send_keys(username)

        pword = driver.find_element_by_id('password-field')
        pword.send_keys(password)

        form = driver.find_element_by_id('loginform')
        form.submit()

        wait_for_id(driver, 'user-nav')

        cookies = driver.get_cookies()
    finally:
        driver.quit()

    cookie_jar = parse_cookie_list(cookies)

    with open('.cookies', 'w') as cookiestore:
        json.dump(cookies, cookiestore)

    return cookie_jar

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: {} <username> <password> [music directory]".format(sys.argv[0]))
        print("  -music directory defaults to './Music'")
        quit()

    username = sys.argv[1]
    password = sys.argv[2]

    if len(sys.argv) == 4:
        music_directory = sys.argv[3]
    else:
        music_directory = "Music"

    session = requests.Session()
    session.cookies = bandcamp_login(username, password)

    parser = ParseUserInfo(session=session)
    parser.get_albums(username)

    albums = get_collection(parser.fan_id, parser.last_token, albums=parser.albums, session=session)
    for album in albums:
        downloader = DownloadAlbum(album, session, base_dir=music_directory)
        downloader.parse_album()
        downloader.fetch_album()
