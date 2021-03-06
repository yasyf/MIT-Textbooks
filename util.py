#!/usr/bin/env python

from functions import *

def reset_class_db(verify=False):
  if not verify:
    return
  all_classes = set()
  for c in classes.find():
    classes.remove(c["_id"])
    all_classes.add(c["class"])
  time.sleep(5)
  for c in all_classes:
    send_to_worker(c)

def transform_all(f, verbose=False):
  for c in classes.find({'error': None}):
    transformations = f(c.copy())
    classes.update({'_id': c['_id']}, transformations)
    print 'Processed', c['class']
    if verbose:
      print transformations

def transform_all_set(f):
  transform_all(lambda x: {"$set": f(x)})

def gen_sorted_classes():
  for c in classes.find({'error': None}):
    c_obj = get_class(c['class'])
    rankings.insert({'class': c['class'], 'title': c_obj.display_name(), 'base_rating': c_obj.get_base_rating(), 'rating': c_obj.get_total_rating()})
    print 'Processed', c['class']
