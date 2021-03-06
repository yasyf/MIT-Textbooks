#!/usr/bin/env python

from setup import *
import json, hashlib, time, datetime, requests, mechanize, Levenshtein, operator, urllib, re, traceback, bleach, csv, StringIO, base64
from hashids import Hashids
from flask import g, flash, url_for, request
from bs4 import BeautifulSoup
from lxml import objectify
from bson.objectid import ObjectId
from models.mitclass import MITClass
from models.mitclassgroup import MITClassGroup
from models.mituser import MITUser
from api import CachedAPI
from coinbase import make_coinbase_request
import HTMLParser

class_objects = {}
group_objects = {}
user_objects = {}
view_objects = {}

auth_browser = None

def init_auth_browser():
  global auth_browser

  auth_browser = mechanize.Browser()
  auth_browser.set_handle_robots(False)
  try:
    auth_browser.open("https://edu-apps.mit.edu/ose-rpt/subjectEvaluationSearch.htm")
    auth_browser.select_form(nr=1)
    auth_browser["j_username"] = os.getenv('j_username')
    auth_browser["j_password"] = os.getenv('j_password')
    auth_browser.submit()
    auth_browser.select_form(nr=0)
    auth_browser.submit()
  except Exception:
    pass

def send_to_worker(class_id, update=False, group=False):
  d = {'class_id': class_id, 'update': update, 'group': group, 'queue': 'worker'}
  if not queue.find_one(d):
    d['time'] = time.time()
    queue.insert(d)

def error_mail(e):
  message = sendgrid.Mail()
  message.add_to(os.getenv('admin_email'))
  message.set_subject('500 Internal Server Error @ MIT Textbooks')
  trace = traceback.format_exc()
  message.set_html(request.url + '<br><br>' + e.message + '<br><br><pre>' + trace + '</pre>')
  message.set_text(request.url + '\n\n' + e.message + '\n\n' + trace)
  message.set_from('MIT Textbooks <tb_support@mit.edu>')
  try:
    print sg.send(message)
  except Exception:
    pass

def sha(text):
  return hashlib.sha256(text).hexdigest()

def md5(s):
  if md5 not in g:
    g.md5 = {}
  if s in g.md5:
    return g.md5[s]
  _hash = hashlib.md5(s).hexdigest()
  g.md5[s] = _hash
  return _hash

def clean_html(html):
  return ' '.join(bleach.clean(html.replace("\n"," ").replace("\t"," "), tags=[], strip=True).encode('ascii','xmlcharrefreplace').split())

def get_user(email, name, create=True):
  global user_objects
  if not email:
    return None
  if email in user_objects:
    return user_objects[email]
  else:
    u = MITUser(email, name, create=create)
    user_objects[email] = u
    return u

def get_class(class_id):
  global class_objects
  class_id = format_class(class_id)
  if class_id in class_objects:
    return class_objects[class_id]
  if not is_worker:
    class_info = classes.find_one({'$and': [{'$or': [{'class': class_id}, {'search_term': { "$in": [class_id.lower()]}}]}, {'error': None}]})
    if class_info:
      if class_info.get('error'):
        return None
      if (time.time() - class_info['textbooks']['dt']) > (CACHE_FOR/4.0):
        send_to_worker(class_id, update=True)
      elif (time.time() - class_info['dt']) > CACHE_FOR:
        send_to_worker(class_id)
      class_obj = MITClass(class_info)
      class_objects[class_id] = class_obj
      return class_obj
  class_info = fetch_class_info(class_id)
  if class_info and not class_info.get('error'):
    class_obj = MITClass(class_info)
    class_objects[class_id] = class_obj
    class_d = class_obj.to_dict()
    class_d['error'] = None
    classes.update({"class": class_obj.id}, {"$set": class_d}, upsert=True)
    class_d = classes.find_one({"class": class_obj.id})
    class_d['objectID'] = str(class_d['_id'])
    algolia.saveObject(class_d)
    return class_obj

  old_class = classes.find_one({'class': class_id})
  if not old_class or not old_class.get('description'):
    classes.update({"class": class_id}, {"$set": {"error": 404, 'dt': time.time()}}, upsert=True)
  else:
    classes.update({"class": class_id}, {"$set": {"semesters": [], 'error': None, 'dt': time.time()}})

def get_embedly_info(class_site):
  endpoint = "http://api.embed.ly/1"
  excludes = {'mit', 'exam', 'recitation', 'homework', 'pset', 'course', 'class', 'stellar'}
  c = {}
  def text_exclude(s):
    s = s.lower()
    if s[0].isdigit():
      return False
    for x in excludes:
      if s in x or x in s:
        return False
    return True

  client = CachedAPI(endpoint, ('key', os.getenv('EMBEDLY_KEY')))
  url = class_site[1]

  result = client.make_request('extract', url=url)

  entities = result.get('entities',[])
  keywords = result.get('keywords',[])

  entities = [x['name'].lower() for x in entities[:5] if text_exclude(x['name'])]
  keywords = [x['name'].lower() for x in keywords[:5] if text_exclude(x['name'])]
  description = result.get('description','')

  c['entities'] = list(set(entities))
  c['keywords'] = list(set(keywords))
  c['description'] = description

  return c


def update_textbooks(class_id):
  try:
    class_info = classes.find_one({'$or': [{'class': class_id}, {'search_term': { "$in": [class_id.lower()]}}]})
    class_info['textbooks'] = get_textbook_info(class_info['class'], class_info['semesters'])
    classes.update({"class": class_info['class']}, {"$set": {'textbooks': class_info['textbooks']}})
    algolia.partialUpdateObject({'objectID': str(class_info['_id']), 'textbooks': class_info['textbooks']})
  except:
    send_to_worker(class_id)

def get_group(group_id):
  global group_objects
  if group_id in group_objects:
    return group_objects[group_id]
  group_info = groups.find_one({"$or": [{"name": group_id}, {"hash": group_id}]})
  if group_info:
    group_obj = MITClassGroup(group_info)
    group_objects[group_id] = group_obj
    return group_obj

def u_strip(s):
  s = unicode(s)
  if s.count('+') > 1:
    s = s[:s.index('+')]
  whitespace_chars = [u'\u202c', u'\ufffd']
  s = s.strip()
  for char in whitespace_chars:
    s = s.replace(char, '')
  return s

def format_class(c, remove_suffix=True):
  if not c:
    return c
  c = u_strip(c)
  c_up = c.upper()
  if re.match(CLASS_REGEX, c_up):
    c = c_up
    if remove_suffix:
      if c[-1] == 'J':
        c = c[:-1]
    return c.split(',')[0].strip()
  else:
    return c

def is_int(value):
  try:
    int(value)
    return True
  except ValueError:
    return False

def prepare_class_hash(classes):
  classes = ','.join(list(sorted(set([format_class(clean_html(c)) for c in classes if len(c) > 1]), key=lambda x: float(x) if is_float(x) else int(x.split('.')[0]) if '.' in x and is_int(x.split('.')[0]) else x)))
  _hash = md5(classes)
  if groups.find_one({"hash": _hash}):
    return _hash
  else:
    group_info = create_group_info(classes, _hash)
    group_obj = MITClassGroup(group_info)
    group_objects[_hash] = group_obj
    groups.insert(group_obj.to_dict())
    return _hash

def create_group_info(classes, _hash):
  group_info = {}
  group_info['named'] = False
  group_info['hash'] = _hash
  group_info['class_ids'] = classes.split(',')
  return group_info

def check_json_for_class(result, class_id):
  json_data = result.get("items")
  relevant = {}
  if json_data:
    _id = clean_html(class_id)
    for element in json_data:
      if element['type'] == 'Class' and element['id'] == _id:
        relevant['class'] = element
      elif element['type'] == 'LectureSession' and element['section-of'] == _id:
        relevant['lecture'] = element
  return relevant

def is_manual_override(class_id):
  return bool(overrides.find_one({'term': TERM, 'reason': 'manual_scrape', 'class': class_id}))

def add_manual_override(class_id):
  overrides.insert({'term': TERM, 'reason': 'manual_scrape', 'class': class_id})

def fetch_class_info(class_id):
  if is_manual_override(class_id):
    class_info = None
  else:
    client = CachedAPI('http://coursews.mit.edu', ('key',''))
    result = client.make_request('coursews', term=TERM, courses=class_id.split('.')[0])
    class_info = check_json_for_class(result, class_id)
    if not class_info:
      result = client.make_request('coursews', term=TERM_LAST, courses=class_id.split('.')[0])
      class_info = check_json_for_class(result, class_id)
  if class_info:
    return clean_class_info(class_info['class'], class_info['lecture'] if 'lecture' in class_info else None)
  else:
    class_info =  manual_class_scrape(class_id)
    if not class_info:
      class_info = manual_class_scrape(class_id, url=LAST_CATALOG)
    if not class_info and not class_id[0].isdigit() and not is_float(class_id) and not re.match(CLASS_REGEX, class_id.upper()):
      try:
        old_class_id = algolia.search(class_id, { "hitsPerPage": 1 })['hits'][0]['objectID']
      except (IndexError, algoliasearch.AlgoliaException):
        return None
      old_class = classes.find_one({'_id': ObjectId(old_class_id)})
      classes.update({'_id': old_class['_id']}, {"$addToSet": {"search_term": class_id.lower()}})
      c = classes.find_one({'class': old_class['class']})
      algolia.partialUpdateObject({'objectID': str(c['_id']), "search_term": c['search_term']})
      class_info = c
    return class_info

def custom_parse_instructors(instructors):
  excludes = ["Jr.", "Sr."]
  new_intructors = []
  for i in instructors:
    new_intructors.extend([x.split(':')[-1] for x in i.split('<br>')])
  return filter(lambda x: x not in excludes, new_intructors)

def custom_shorten_name(name):
  replacements = [("Introduction", "Intro"), ("Principles of ", ""), ("Introductory", "Intro"), ("Chemistry", "Chem"), ("Biology", "Bio")]
  for k,v in replacements:
    name = name.replace(k,v)
  return name

def manual_class_scrape(class_id, url=CURRENT_CATALOG):
  try:
    class_info = {}
    class_info['dt'] = int(time.time())
    url = url.format(class_id=class_id)
    html = requests.get(url).text
    if 'No matching subjects found.' in html:
      return
    soup = BeautifulSoup(html)
    ssoup = str(soup)
    name = clean_html(soup.find("h3").text).split(' ')
    class_info['class'], class_info['name'] = name[0], ' '.join(name[1:])
    class_info['short_name'] = custom_shorten_name(class_info['name'])
    class_info['master_subject_id'] = class_info['class']
    class_info['course'] = class_info['class'].split('.')[0]

    old_class = classes.find_one({'class': class_info['class'], 'description': {'$ne': None}})
    if old_class and class_id != class_info['class']:
      classes.update({'_id': old_class['_id']}, {"$set": {'error': None}, "$addToSet": {"search_term": class_id.lower()}})
      c = classes.find_one({'class': old_class['class']})
      algolia.partialUpdateObject({'objectID': str(c['_id']), "search_term": c['search_term']})
      return c

    when = []
    for when_when,when_img in {"Fall": "fall", "IAP": "iap", "Spring": "spring", "Summer": "summer"}.iteritems():
      when_img_src = "/icns/%s.gif" % (when_img)
      if soup.find("img",src=when_img_src) != None:
        when.append(when_when)
    class_info['semesters'] = when
    class_info['hass'] = ''
    class_info['ci'] = ''
    for img in soup.find_all('img'):
      if img.get('src') == "/icns/cihw.gif":
        class_info['ci'] = 'HW'
      elif img.get('src') == "/icns/cih1.gif":
        class_info['ci'] = 'H'
      elif img.get('src') == "/icns/hassS.gif":
        class_info['hass'] = 'S'
      elif img.get('src') == "/icns/hassA.gif":
        class_info['hass'] = 'A'
      elif img.get('src') == "/icns/hassH.gif":
        class_info['hass'] = 'H'
      elif img.get('src') == "/icns/grad.gif":
        class_info['grad'] = True
      elif img.get("src") == "/icns/hr.gif":
        nextTag = img.findNext()
        if nextTag.name == "br":
          text = ""
          while nextTag.nextSibling.name != "br":
            text += clean_html(nextTag.nextSibling)
            nextTag = nextTag.nextSibling
          instructors = nextTag.findNext().findNext().text
          all_instructors = []
          for professor in instructors.split(", "):
            if professor not in ["Jr.","Sr."]:
              all_instructors.append(professor)
          final_instructors = [clean_html(i) for i in custom_parse_instructors(all_instructors)]
          for instructor in final_instructors:
            search_google(instructor + ' + MIT')
          class_info['instructors'] = {k.lower():(final_instructors if k in when else []) for k in ['Fall', 'Spring']}

    prereq_index_start = ssoup.find("Prereq: ") + 8
    if prereq_index_start:
      prereq_index_end = ssoup.find("<br/>",prereq_index_start)-1
      prereq_info = clean_html(ssoup[prereq_index_start:prereq_index_end])
    else:
      prereq_info = ''
    class_info['prereqs'], class_info['coreqs']  = process_prereqs(prereq_info)
    unit_index_start = ssoup.find("Units: ") + 7
    unit_index_end = unit_index_start + 5
    units_info = clean_html(ssoup[unit_index_start:unit_index_end])
    class_info['units'] = [int(x) for x in units_info.split('-')]
    lecture_node = soup.find('b', text='Lecture:')
    if lecture_node:
      class_info['lecture'] = clean_html(lecture_node.nextSibling.nextSibling.text)
      class_info['location'] = clean_html(lecture_node.nextSibling.nextSibling.nextSibling.nextSibling.text)
    else:
      class_info['lecture'] = ''
      class_info['location'] = ''
    class_info['description'] = text
    class_info['stellar_url'] = get_stellar_url(class_id)
    class_info['class_site'] = get_class_site(class_id)
    class_info['evaluation'] = get_subject_evaluation(class_id)
    class_info['textbooks'] = get_textbook_info(class_id, class_info['semesters'])
    class_info['meta'] = get_embedly_info(class_info['class_site'])
    class_info['search_term'] = [class_id.lower()]
    return class_info
  except Exception:
    return

def clean_class_info(class_info, lecture_info):
  class_info_cleaned = {}
  class_info_cleaned['dt'] = int(time.time())
  class_info_cleaned['class'] = class_info['id']
  class_info_cleaned['master_subject_id'] = class_info['master_subject_id'] if 'master_subject_id' in class_info else class_info['id']
  class_info_cleaned['course'] = class_info['course']
  class_info_cleaned['name'] = clean_html(class_info['label'])
  class_info_cleaned['prereqs'], class_info_cleaned['coreqs'] = process_prereqs(clean_html(class_info['prereqs']))
  class_info_cleaned['short_name'] = custom_shorten_name(clean_html(class_info['shortLabel']))
  class_info_cleaned['description'] = clean_html(class_info['description'])
  class_info_cleaned['semesters'] = class_info['semester']
  class_info_cleaned['hass'] = class_info['hass_attribute'][-1:]
  class_info_cleaned['ci'] = class_info['comm_req_attribute'].replace('CI','')
  class_info_cleaned['units'] = [int(x) for x in class_info['units'].split('-')]
  class_info_cleaned['instructors'] = {'spring': [clean_html(i) for i in custom_parse_instructors(class_info['spring_instructors'])], 'fall': [clean_html(i) for i in custom_parse_instructors(class_info['fall_instructors'])]}
  class_info_cleaned['stellar_url'] = get_stellar_url(class_info['id'])
  class_info_cleaned['class_site'] = get_class_site(class_info['id'])
  class_info_cleaned['evaluation'] = get_subject_evaluation(class_info['id'])
  class_info_cleaned['textbooks'] = get_textbook_info(class_info['id'], class_info_cleaned['semesters'])
  class_info_cleaned['meta'] = get_embedly_info(class_info_cleaned['class_site'])
  class_info_cleaned['search_term'] = []
  if lecture_info:
    data = lecture_info['timeAndPlace'].split(' ')
    class_info_cleaned['lecture'], class_info_cleaned['location'] = clean_html(' '.join(data[:-1])), clean_html(data[-1])
  else:
    class_info_cleaned['lecture'], class_info_cleaned['location'] = '', ''

  if class_info_cleaned['course'] == '6':
    eecs_staff = get_eecs_staff(class_info_cleaned['class'])
    class_info_cleaned['instructors']['spring'] = eecs_staff or class_info_cleaned['instructors']['spring']
    class_info_cleaned['instructors']['fall'] = eecs_staff or class_info_cleaned['instructors']['fall']

  excludes = ['staff']
  def test_instructor(instructor):
    instructor = instructor.lower()
    for phrase in excludes:
      if phrase in instructor:
        return False
    return True

  for key, instructor_set in class_info_cleaned['instructors'].iteritems():
    class_info_cleaned['instructors'][key] = [instructor for instructor in instructor_set if test_instructor(instructor)]

  for instructor_set in class_info_cleaned['instructors'].values():
    for instructor in instructor_set:
      search_google(instructor + ' + MIT')

  return class_info_cleaned

def get_eecs_staff(c):
  r = requests.get('https://eecs.scripts.mit.edu/eduportal/who_is_teaching_what_data_out/F/{y}/'.format(y=datetime.date.today().year))
  if r.status_code != requests.codes.ok:
    return []
  data = r.text
  reader = csv.DictReader(StringIO.StringIO(data), fieldnames=['class','name','first_name','last_name','title'])
  instructors = []
  for row in reader:
    if row['class'] == c and 'Lecturer' in row['title']:
      instructors.append(row['first_name'] + ' ' + row['last_name'])
  def initialize(instructor):
    l = clean_html(instructor).split(' ')
    return ' '.join([x[0] + '.' for x in l[:-1]]) + ' ' + l[-1]
  return [initialize(x) for x in instructors]

def process_prereqs(prereqs):
  if not prereqs:
    return [],[]
  d = {'GIR:PHY1': '8.01', 'GIR:CAL1': '18.01','GIR:PHY2': '8.02', 'GIR:CAL2': '18.02', 'GIR:BIOL': '7.01x', 'GIR:CHEM': '3.091 or 5.111'}
  for k,v in d.iteritems():
    prereqs = prereqs.replace(k, v)
  coreqs = re.findall(re.compile(r'\[([\w]{1,3}\.(?:(?:[sS]?[0-9]{2,3}[\w]{0,1})|(?:UA[TR])))\]'), prereqs)
  prereqs = re.sub(re.compile(r'\[[\w]{1,3}\.(?:(?:[sS]?[0-9]{2,3}[\w]{0,1})|(?:UA[TR]))'), '', prereqs)
  prereqs = re.findall(re.compile(r'([\w]{1,3}\.(?:(?:[sS]?[0-9]{2,3}[\w]{0,1})|(?:UA[TR])))'), prereqs)
  prereqs, coreqs = [x.strip() for x in prereqs], [x.strip() for x in coreqs]
  return prereqs, coreqs

def update_recents_with_class(class_obj):
  recent_entry = recents.find_one({'class': class_obj.id})
  if recent_entry:
    if (datetime.datetime.utcnow() - recent_entry['dt']) > datetime.timedelta(minutes=1):
      recents.update({'class': class_obj.id}, {'$set':{'dt': datetime.datetime.utcnow()}})
  else:
    recents.insert({'class': class_obj.id, 'dt': datetime.datetime.utcnow(), 'display_name': class_obj.display_name(), 'description': class_obj.summary(), 'rating': class_obj.get_base_rating()})
  if g.user:
    g.user.add_recent_class(class_obj.id)

def get_stellar_url(class_id):
  url = "https://stellar.mit.edu/S/course/%s/%s/%s/" % (class_id.split('.')[0], STERM, class_id)
  r = requests.get(url)
  if r.url == 'https://stellar.mit.edu/stellar-error/404.html':
    url = "https://stellar.mit.edu/S/course/%s/%s/%s/" % (class_id.split('.')[0], STERM_LAST, class_id)
    r = requests.get(url)
  if r.url == 'https://stellar.mit.edu/stellar-error/404.html':
    return None
  else:
    return r.url

def try_url(url):
  try:
    return requests.get(url)
  except requests.exceptions.SSLError:
    return requests.get(url, verify=False, cert='cert.pem')
  except requests.exceptions.TooManyRedirects:
    return None
  except requests.exceptions.ConnectionError:
    time.sleep(1)
    return None

def get_google_url(term):
  return "http://www.google.com/search?&q={q}&btnG=Google+Search&inurl=https".format(q=urllib.quote_plus(term))

def search_google(term, cache=True):
  cached = google_cache.find_one({'term': term})
  if cache and cached and cached['dt'] > (datetime.datetime.utcnow() - datetime.timedelta(seconds=CACHE_FOR)):
    return cached['links']

  br = mechanize.Browser()
  br.addheaders = [('User-agent', 'Mozilla/5.0 (X11; Linux x86_64; rv:18.0) Gecko/20100101 Firefox/18.0 (compatible;)'),('Accept', '*/*')]
  br.set_handle_robots(False)
  google_url = get_google_url(term)
  try:
    br.open(google_url)
  except:
    return []
  def check_excludes(url):
    excludes = ['google', 'youtube', 'blogger']
    for ex in excludes:
      if ex in url:
        return False
    return True
  links = [link.url for link in br.links() if check_excludes(link.url) and link.url[:4] == 'http']
  google_cache.update({'term': term}, {"$set": {'links': links, 'dt': datetime.datetime.utcnow()}}, upsert=True)
  return links

def get_google_site_guess(class_id):
  def process_urls(urls):
    for url in urls:
      if 'mit.edu' in url and 'stellar' not in url and 'textbooksearch' not in url:
        get = try_url(url)
        if get:
          return get
  term = 'MIT + ' + class_id
  result = process_urls(search_google(term))
  if result:
    return result
  result = process_urls(search_google(term, cache=False))
  if result:
    return result
  return try_url(get_google_url(term))

def follow_redirects(r):
  soup = BeautifulSoup(r.text)
  meta = soup.find('meta', attrs={'http-equiv': 'refresh'})
  try:
    url = meta['content'].split("=")[-1]
    new_r = try_url(url) or r
    return new_r
  except:
    return r

def get_class_site(class_id):
  url = "http://course.mit.edu/{class_id}".format(class_id=class_id)
  r = try_url(url)
  if r is None or 'stellar' in r.url or 'course.mit.edu' in r.url:
    r = get_google_site_guess(class_id)
  r = follow_redirects(r)
  soup = BeautifulSoup(r.text)
  try:
    title = soup.find('title').string
    if 'no title' in title.lower():
      title = "{class_id} Class Site".format(class_id=class_id)
  except AttributeError:
    title = "{class_id} Class Site".format(class_id=class_id)
  title = title.split('|')[0].strip()
  return (clean_html(title), r.url)

def get_subject_evaluation(class_id):
  all_info = {}
  try:
    url = "https://edu-apps.mit.edu/ose-rpt/subjectEvaluationSearch.htm?subjectCode={class_id}&search=Search".format(class_id=class_id)
    try:
      response = auth_browser.open(url)
    except Exception:
      response = auth_browser.open(url)
    response_text = response.read()
    if 'Welcome, please identify yourself to access MIT services.' in response_text:
      init_auth_browser()
      response = auth_browser.open(url)
      response_text = response.read()
    soup = BeautifulSoup(response_text)
    for i,link in enumerate(auth_browser.links()):
      if 'subjectEvaluationReport' in link.url:
        date = str(soup.findAll('a')[i].next_sibling).replace("End of Term","").strip()
        response = auth_browser.follow_link(link)
        break
    soup = BeautifulSoup(response.read())
    rating = soup.find('strong', text='Overall rating of subject: ')
    percentage = soup.find('strong', text='Response rate:')
    all_info['date'] = date
    all_info['rating'] = float(clean_html(str(rating.next_sibling))[:3])
    all_info['percent_response'] = float(clean_html(str(percentage.next_sibling))[:-1])
    pairings = {"Subject expectations were clearly defined": "expectations_clear", "Subject expectations were made clear": "expectations_clear", "Subject's learning objectives were met": "learning_objectives_met", "Assignments contributed to my learning": "assigments_useful", "Grading thus far has been fair": "grading_fair", "The pace of the class (content and assignments) was:": "pace", "Average hours you spent per week on this subject in the classroom": "classroom_hours", "Average hours you spent per week on this subject outside of the classroom": "home_hours", "Lab hours/week": "lab_hours", "Prep hours/week": "prep_hours", "Average hours you spent per week on this subject, both in and outside the classroom": 'hours'}
    for string, key in pairings.iteritems():
      try:
        found = soup.find(text=string).parent
        elt = found.parent if found.name == "a" else found
        all_info[key] = float(clean_html(str(elt.next_sibling.next_sibling)))
      except Exception:
        continue
    return all_info
  except Exception:
    return all_info

def get_textbook_info(class_id, semesters):
  time.sleep(.5)
  pairing = {'SP': 'Spring', 'FA': 'Fall'}
  if pairing[TERM[-2:]] in semesters:
    term = TERM
    term_l = TERM_LAST
  else:
    term = TERM_LAST
    term_l = TERM

  textbooks = {'dt': time.time()}
  sections = {}
  titles = set()
  asin = set()

  url = "http://sisapp.mit.edu/textbook/books.html?Term={term}&Subject={class_id}".format(term=term, class_id=class_id)
  html = requests.get(url).text
  if 'No text books are recorded for your request.' in html:
    url = "http://sisapp.mit.edu/textbook/books.html?Term={term}&Subject={class_id}".format(term=term_l, class_id=class_id)
    html = requests.get(url).text
  if 'No text books are recorded for your request.' in html:
    textbooks["sections"] = sections
    return textbooks
  soup = BeautifulSoup(html)

  for h2 in soup.findAll('h2'):
    book_category = []
    tbody = h2.next_sibling.next_sibling.contents[3]
    for tr in tbody.findAll('tr'):
      book = {}
      contents = filter(lambda x: x != '\n', tr.contents)
      for i, prop in enumerate(['author', 'title', 'publisher', 'isbn', 'price']):
        book[prop] = clean_html(contents[i].text)
      if 'Course Has No Materials' in book['title']:
        continue
      book['title'] = process_title(book['title'], book['author'], titles)
      book['retail'] = book['price'].replace("$","").replace(',','')
      del book['price']
      amazon_info = get_amazon_info(book['isbn'], book['title'], book['author'])
      book = dict(book.items() + amazon_info.items())
      abe_info = get_abe_info(book['isbn'])
      update_new_prices(book, abe_info)
      if book['asin'] not in asin:
        book_category.append(book)
        asin.add(book['asin'])
    if len(book_category) > 0:
      sections[clean_html(h2.string)] = book_category
  textbooks["sections"] = sections
  return textbooks

def get_abe_info(isbn):
  d = {}
  try:
    url = "http://search2.abebooks.com/search?clientkey={key}&outputsize=short&isbn={isbn}&bookcondition=newonly".format(key=os.getenv('abe_key'), isbn=isbn)
    response = str(requests.get(url).text)
    root = objectify.fromstring(response)
    d['new'] = root.Book.listingPrice.text
    listing_url = urllib.quote_plus('http://'+root.Book.listingUrl.text)
    d['purchase'] = ('AbeBooks', "http://affiliates.abebooks.com/c/92729/77416/2029?u={u}".format(u=listing_url))
  except Exception:
    pass
  time.sleep(.5)
  try:
    url = "http://search2.abebooks.com/search?clientkey={key}&outputsize=short&isbn={isbn}&bookcondition=usedonly".format(key=os.getenv('abe_key'), isbn=isbn)
    response = str(requests.get(url).text)
    root = objectify.fromstring(response)
    d['used'] = root.Book.listingPrice.text
    listing_url = urllib.quote_plus('http://'+root.Book.listingUrl.text)
    d['purchase'] = ('AbeBooks', "http://affiliates.abebooks.com/c/92729/77416/2029?u={u}".format(u=listing_url))
  except Exception:
    pass
  return d

def update_new_prices(book, info):
  for kind in ['new', 'used']:
    if kind in info:
      if not kind in book or float(info[kind]) < float(book[kind]):
        book[kind] = info[kind]
        book['purchase'] = info['purchase']
  return book

def doItemSearch(Keywords,SearchIndex):
  i = 1
  while i <= 5:
    try:
      response = amazon.ItemSearch(Keywords=Keywords, SearchIndex=SearchIndex)
      return response
    except Exception:
      time.sleep(.5*i)
      i += 1

def doItemLookup(ItemId,ResponseGroup):
  i = 1
  while i <= 5:
    try:
      response = amazon.ItemLookup(ItemId=ItemId, ResponseGroup=ResponseGroup)
      return response
    except Exception:
      time.sleep(.5*i)
      i += 1

def get_amazon_info(isbn, title, author):
  if '[Ebook]' in title:
    title = title.replace("[Ebook]","")
    title = "{title} ebook".format(title=title)
  d = {'asin': None}
  response = doItemSearch(Keywords=isbn, SearchIndex="Books")
  if not response:
    return d
  root = objectify.fromstring(response)
  if root.Items.TotalResults == 0:
    response = doItemSearch(Keywords="{title} by {author}".format(title=title, author=author), SearchIndex='All')
    if not response:
      return d
    root = objectify.fromstring(response)
  if root.Items.TotalResults == 0:
    response = doItemSearch(Keywords=title, SearchIndex='All')
    if not response:
      return d
    root = objectify.fromstring(response)
  try:
    response = doItemLookup(ItemId=root.Items.Item.ASIN.text, ResponseGroup='ItemAttributes,Offers,OfferSummary,Images')
    if not response:
      return d
    product = objectify.fromstring(response).Items.Item
  except AttributeError:
    return d
  d['asin'] = product.ASIN.text
  d['title'] = product.ItemAttributes.Title.text

  try:
    d['image'] = product.LargeImage.URL.text
  except AttributeError:
    pass
  try:
    d['author'] = product.ItemAttributes.Author.text
  except AttributeError:
    pass
  try:
    d['new'] = (product.OfferSummary.LowestNewPrice.FormattedPrice.text).replace("$","").replace(',','')
    d['used'] = (product.OfferSummary.LowestUsedPrice.FormattedPrice.text).replace("$","").replace(',','')
    d['availability'] = product.Offers.Offer.OfferListing.Availability.text
  except AttributeError:
    pass
  try:
    d['saved'] = product.Offers.Offer.OfferListing.PercentageSaved.text
  except AttributeError:
    pass
  try:
    d['prime'] = True if product.Offers.Offer.OfferListing.IsEligibleForSuperSaverShipping.text == 1 else False
  except AttributeError:
    pass
  return d

def process_title(title, author, titles):

  replacements = {"W/6 Mo": "With 6 Month", " + ": " and ", "+": " and "}
  removals = ["4e", "(Cs)", ">Ic", "and Study Guide"]

  if "Ebk" in title and len(titles) > 0:
    Levenshtein_ratios = dict()
    for t in titles:
      Levenshtein_ratios[t[0]] = Levenshtein.ratio(t[1], title + " by " + author)
    title = max(Levenshtein_ratios.iteritems(), key=operator.itemgetter(1))[0]
    return "[Ebook] " + title

  else:
    for k,v in replacements.iteritems():
      title = title.replace(k,v)
    for v in removals:
      title = title.replace(v,"")
    if title not in titles:
      titles.add((title, title + " by " + author))
    return title.strip()

def check_class_json(class_id):
  loaded = check_class(class_id)
  return {'loaded': loaded}

def check_class(class_id, for_errors=False):
  if class_id is None:
    return False
  if for_errors:
    loaded = classes.find_one({'$and': [{'$or': [{'class': class_id}, {'search_term': { "$in": [class_id.lower()]}}]}, {'error': None}]})
  else:
    loaded = classes.find_one({'$or': [{'class': class_id}, {'search_term': { "$in": [class_id.lower()]}}]}) != None
  return loaded

def check_group(class_ids):
  loaded = False not in [check_class(class_id) for class_id in class_ids]
  return loaded

def save_group(group_obj, group_name):
  global group_objects
  if not g.user:
    return {"error": True, "message": "You must be logged in to do that."}
  group_name = group_name.replace(' ','')
  if not re.match('^[\w]+$', group_name):
    return {"error": True, "message": "The group name must be alphanumeric."}
  if groups.find_one({"name": group_name}):
    return {"error": True, "message": "That group name is already taken."}
  group_info = {}
  group_info['named'] = True
  group_info['name'] = group_name
  group_info['user_id'] = g.user.get_id()
  group_info['class_ids'] = group_obj.class_ids
  named_group_obj = MITClassGroup(group_info)
  group_objects[group_name] = named_group_obj
  groups.insert(named_group_obj.to_dict())
  flash('{name} was successfully created!'.format(name=group_name), 'success')
  return {"error": False}

def tb_id(textbook):
  if textbook['asin']:
    return textbook['asin']
  elif textbook['isbn']:
    return textbook['isbn']
  else:
    return md5(textbook['title'])

def get_mit_info(email):
    username = email[:email.find("@")]
    url = "http://web.mit.edu/bin/cgicso?options=general&query=%s" % (username)
    r = requests.get(url)
    html = r.text
    soup = BeautifulSoup(html)
    pre = soup.find("pre")
    if pre.text.find("No matches") == -1:
      l = [[y.strip() for y in x.split(":")] for x in pre.text.split("\n")]
      d = {x[0]:x[1] for x in l if len(x) is 2}
      return d.get('name'), None, d.get('year')

def tb_id_to_tb(class_id, textbook_id):
  class_obj = get_class(class_id)
  for section in class_obj.textbooks['sections'].values():
    for book in section:
      if tb_id(book) == textbook_id:
        return book

def get_button(class_id, tb_id):
  tb = tb_id_to_tb(class_id, tb_id)
  if not tb:
    return {'button': {}}
  d = {'tb_id': tb_id, 'class_id': class_id}
  button = buttons.find_one(d)
  if button is not None:
    return button
  url = url_for('class_view', class_id=class_id, _external=True)
  try:
    price = float(tb.get('new', tb.get('used', tb.get('retail'))))
  except ValueError:
    return {'button': {}}
  params =  {
    'button': {
      'name' : tb['title'],
      'type': 'buy_now',
      'custom': json.dumps({'tb_id': tb_id, 'class_id': class_id, 'user': g.user.get_id() if g.user else None}),
      'price_string' : str(price*1.15),
      'description': u'{} by {} from MIT Textbooks ({})'.format(tb['title'], tb['author'], tb.get('availability', 'Ship as soon as possible.')),
      'price_currency_iso' : 'USD',
      'style': 'none',
      'include_email': True,
      'collect_email': True,
      'include_address': True,
      'collect_shipping_address': True,
      'custom_secure': True,
      'success_url': url,
      'cancel_url': url,
      'info_url': url
    }
  }
  resp = make_coinbase_request('https://coinbase.com/api/v1/buttons', body=json.dumps(params))
  d.update(resp)
  if 'button' not in d:
    return {'button': {}}
  buttons.insert(d)
  return d

def sell_textbook(class_id, tb_id, form):
  d = {}
  d['tb_id'] = tb_id
  d['price'] = int(form.get('price'))
  d['class_id'] = class_id
  d['location'] = form.get('location')
  d['dt'] = datetime.datetime.utcnow()
  d['email'] = form.get('email')
  d['condition'] = form.get('condition')
  info = get_mit_info(form.get('email'))
  if info:
    d['name'] = info[0]
    d['address'] = info[1]
    d['year'] = info[2]
  offers.insert(d)

def remove_offer(offer_id):
  offer = offers.find_one({"_id": ObjectId(offer_id)})
  if offer and g.user and g.user.get_id() == offer['email']:
    offers.remove({"_id": ObjectId(offer_id)})

def delete_group(group_id):
  group = groups.find_one({"name": group_id})
  if group and g.user.get_id() == group['user_id']:
    groups.remove({"name": group_id})

def activate_group(group_id):
  group = groups.find_one({"name": group_id})
  if group and g.user.get_id() == group['user_id']:
    groups.update({"_id": group['_id']}, {'$set': {'active': True}})

def blacklist_class(class_id):
  b = blacklist.find_one({'class_id': class_id})
  if b:
    blacklist.update({'class_id': class_id}, {"$inc": {"counter": 1}})
    if b['counter'] + 1 >= min(3, b['delay']):
      blacklist.update({'class_id': class_id}, {"$inc": {"delay": 1}, "$set": {"counter": 0}})
  else:
    blacklist.insert({'class_id': class_id, 'delay': 2, 'counter': 0})

def unblacklist_class(class_id):
  b = blacklist.find_one({'class_id': class_id})
  if b:
    blacklist.update({'class_id': class_id}, {"$inc": {"counter": -1}})
    if b['counter'] - 1 < 0:
      blacklist.update({'class_id': class_id}, {"$inc": {"delay": -1}, "$set": {"counter": 0}})

def get_blacklist(classes):
  penalty = 1
  for c in classes:
    b = blacklist.find_one({"class_id": c})
    if b:
      penalty *= b['delay']
  return 1 + (penalty-1)/2.5

def sitemap_allows():
  allows = [url_for('index_view', _external=True), url_for('textbooks_view', _external=True)]
  for c in classes.find({"error": None}):
    if 'textbooks' not in c:
      continue
    allows.append(url_for('class_view', class_id=c['class'], _external=True))
    allows.append(url_for('overview_view', class_id=c['class'], _external=True))
    if 'google.com' not in c['class_site']:
      allows.append(url_for('site_view', class_id=c['class'], _external=True))
    if c['stellar_url']:
      allows.append(url_for('stellar_view', class_id=c['class'], _external=True))
    allows.append(url_for('class_evaluation_view', class_id=c['class'], _external=True))
    for section in c['textbooks']['sections'].values():
      for book in section:
        if 'asin' in book and book['asin']:
          allows.append(url_for('amazon_product_view', asin=book['asin'], _external=True))
  for gr in groups.find({}):
    allows.append(url_for('group_view', group_id=gr['name'] if 'name' in gr else gr['hash'], _external=True))
  return allows

def check_all_times(classes):
  free = {}
  for j in list('MTWRF'):
    free[j] = {}
    for i in range(7,18):
      free[j][i] = []
      free[j][i+0.5] = []
  overlap = set()
  for c in classes:
    lecture = c.lecture.split(',')
    for group in lecture:
      m = re.match(re.compile(TIME_REGEX), group)
      if m:
        for day in list(m.group(1)):
          start_hour = int(m.group(2))
          if start_hour < 7:
            start_hour += 12
          if m.group(3) == '30':
            start_hour += 0.5
          end_hour = int(m.group(4)) if m.group(4) else start_hour + 1
          if end_hour < 7:
            end_hour += 12
          if m.group(5) == '30':
            end_hour += 0.5
          current = start_hour
          while current < end_hour:
            free[day][current].append(c.id)
            if len(free[day][current]) > 1:
              overlap.add(tuple(free[day][current]))
            current += 0.5
  return overlap

def is_float(n):
  try:
    float(n)
    return True
  except ValueError:
    return False

def suggestion(search_term):
  results = recents.find({'class':{'$regex':'^'+format_class(search_term)}}).limit(3)
  suggestions = []
  for r in results:
    suggestions.append({'c': r['class'], 'n': r['display_name']})
  return {'suggestions': suggestions}

def popover(class_id):
  c = recents.find_one({'class': class_id})
  pending = False
  if not c:
    if check_class(class_id):
      cl = get_class(class_id)
      if cl:
        c = cl.to_recents_dict()
    else:
      send_to_worker(class_id)
      pending = True
  if c:
    r = c['evaluation'].get('rating', 0) if 'evaluation' in c else c.get('rating', 0)
    d = {'n': c['display_name'], 'd': c['description'], 'c': class_id, 'r': r}
  else:
    d = None
  return {'class_info': d, 'pending': pending}

def upload_static(app):
  import flask_s3
  flask_s3.create_all(app)

def get_asin_from_hash(_hash):
  return base64.b64decode(_hash)

def get_sorted_classes(original_filters):
  if not original_filters:
    return {}
  key_replacements = {'prereq': 'prereqs', 'coreq': 'coreqs'}
  val_replacements = {'true': True, 'false': False, 'none': None}
  filters = {}
  for key, value in original_filters.iteritems():
    try:
      value = value.split(',')
      value = [val_replacements[v.lower()] if v.lower() in val_replacements else v for v in value]
      key = key.lower()
      if key in key_replacements:
        key = key_replacements[key]
    except AttributeError:
      value = [value]
    filters[key] = value
  all_classes = []
  proto = classes.find_one({key:{'$exists': True} for key in filters})
  if not proto:
    return []
  constraints = []
  for key, value in filters.iteritems():
    if hasattr(proto[key], '__iter__'):
      constraints.append({key: {'$in': value}})
    else:
      constraints.append({'$or': [{key: v} for v in value]})
  good_classes = set([x['class'] for x in classes.find({'$and': constraints})])
  for c in rankings.find().sort('rating', -1):
    if c['class'] in good_classes:
      all_classes.append(c)
  return all_classes

def gen_short_url(view, args):
  hashids = Hashids(salt=view+str(hash(frozenset(args.items()))))
  _hash = hashids.encrypt(len(args))
  previous = shortlinks.find_one({'hash': _hash}) or shortlinks.find_one({'view': view, 'args': args})
  if not previous:
    shortlinks.insert({'view': view, 'args': args, 'hash': _hash})
  else:
    _hash = previous['hash']
  return url_for('short_url_view', _hash=_hash, _external=True)

def expand_short_url(_hash):
  info = shortlinks.find_one({'hash': _hash})
  if info:
    info['args']['_external'] = True
    return url_for(info['view'], **info['args'])
  else:
    return url_for('_404_view')

def view_classes(class_ids):
  identifier = g.user.get_id() if g.user else g.ip
  history = view_objects.get(identifier, [])
  class_ids = list(set(class_ids) - set(history))
  if class_ids:
    classes.update({'class': {'$in': class_ids}}, {'$inc': {'views': 1}})
    history += class_ids
    view_objects[identifier] = history

def mail_password(user):
  message = sendgrid.Mail()
  message.add_to(user.email)
  message.set_subject('Your MIT Textbooks Mobile Password')
  password = user.get_password()
  message.set_html('<pre>' + password + '</pre>')
  message.set_text(password)
  message.set_from('MIT Textbooks <tb_support@mit.edu>')
  try:
    sg.send(message)
  except Exception, e:
    pass

def mail_order(order):
  custom = HTMLParser.HTMLParser().unescape((order['custom']))
  info = json.loads(custom)
  tb = tb_id_to_tb(info['class_id'], info['tb_id'])
  message = sendgrid.Mail()
  message.add_to(order['customer']['email'])
  message.add_bcc('tb_support@mit.edu')
  message.set_subject('Your MIT Textbooks Order')
  address_text = "\n".join(order['customer']['shipping_address'])
  address_html = "<pre>{}</pre>".format(address_text)
  message_text = u"""This is a confirmation of your purchase of {} by {} from MIT Textbooks. Your total was ${} {}. Your order identifier is {}.
We will ship your textbook to the following address as soon as possible.
{}"""
  message_text = message_text.format(tb['title'], tb['author'], order['total_native']['cents']/100, order['total_native']['currency_iso'], order['id'], address_text)
  message_html = message_text.replace("\n", "<br>").format(tb['title'], tb['author'], order['total_native']['cents']/100, order['total_native']['currency_iso'], address_html)
  message.set_html(message_html)
  message.set_text(message_text)
  message.set_from('MIT Textbooks <tb_support@mit.edu>')
  try:
    sg.send(message)
  except Exception, e:
    pass
