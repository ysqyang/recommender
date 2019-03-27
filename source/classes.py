# class definitions
import re
import time
from datetime import datetime, timedelta
import os
from collections import defaultdict
import math
import json
from gensim import corpora, matutils
from gensim.models import tfidfmodel, ldamodel
#from gensim.similarities import Similarity
from gensim.models import Word2Vec
import numpy as np
import jieba
from utils import insert


class TextPreprocessor(object):
    def __init__(self, singles, puncs, punc_frac_low, punc_frac_high,
                 valid_count, valid_ratio, stopwords):
        self.singles = singles
        self.puncs = puncs
        self.punc_frac_low = punc_frac_low
        self.punc_frac_high = punc_frac_high
        self.valid_count = valid_count
        self.valid_ratio = valid_ratio
        self.stopwords = stopwords

    def preprocess(self, text):
        '''
        Tokenize a Chinese document to a list of words
        Args:
        text: text to be tokenized
        '''
        cnt = 0
        for c in text:
            if c in self.puncs:
                cnt += 1

        ratio = cnt / len(text)

        if ratio < self.punc_frac_low or ratio > self.punc_frac_high:
            return []

        alphanum, whitespace = r'\\*\w+', r'\s'
        word_list = []
        words = jieba.cut(text, cut_all=False)

        for word in words:
            if len(word) == 1 \
                    or re.match(alphanum, word, flags=re.ASCII) \
                    or re.match(whitespace, word, flags=re.ASCII) \
                    or word in self.stopwords \
                    or any(c in self.singles for c in word) \
                    or len(word) / len(set(word)) > 2:
                continue
            word_list.append(word)

        if len(word_list) < self.valid_count \
                or len(word_list) / len(set(word_list)) > self.valid_ratio: \
                return []

        return word_list


class Corpus(object):
    '''
    Corpus object
    '''
    def __init__(self, name, logger):
        self.data = {}
        self.dictionary = corpora.Dictionary([])
        self.name = name
        self.logger = logger

    @property
    def size(self):
        return len(self.data)

    def load(self, save_dir):       
        folders = os.listdir(save_dir)
        for folder in folders:
            if not folder.isnumeric():
                continue
            files = os.listdir(os.path.join(save_dir, folder))
            for file in files:
                if not file.isnumeric():
                    continue
                path = os.path.join(save_dir, folder, file)
                try:
                    with open(path, 'r') as f:
                        rec = json.load(f)
                        self.data[file] = {'date': rec['date'],
                                           'body': rec['body'],
                                           'updated': False
                                           }
                except json.JSONDecodeError:
                    self.logger.error('Failed to load topic %s', file)

        self.logger.info('%d topics loaded from disk', len(self.data))

        if len(self.data) > 0:            
            corpus = [data['body'] for data in self.data.values()]
            self.dictionary = corpora.Dictionary(corpus)
            self.logger.info('Dictionary created')

    def add(self, topic_id, content, date):
        if topic_id in self.data:
            self.logger.warning('Topic already exists. Ignoring...')
            return False

        if len(content) == 0:
            self.logger.info('Topic not recommendable')
            return False      
        
        self.dictionary.add_documents([content])

        self.data[topic_id] = {'date': date, 'body': content, 'updated': False}
                     
        self.logger.info('New topic has been added to collection %s', self.name)
        self.logger.info('Corpus data have been updated for collection %s', self.name)
        self.logger.info('%d topics available in collection %s', len(self.data), self.name)
        
        return True

    def delete(self, topic_id):
        if topic_id in self.data:
            del self.data[topic_id]
            self.logger.info('Topic %s has been deleted from the collection', topic_id)
            self.logger.info('Corpus data have been updated')
            self.logger.info('%d topics remaining', len(self.data))

    @ property
    def oldest(self):
        if len(self.data) == 0:
            return None
        return min(self.data.keys())

    @ property
    def latest(self):
        if len(self.data) == 0:
            return None
        return max(self.data.keys())


class CorpusTfidf(Corpus):
    def __init__(self, name, target_corpus, tfidf_scheme, num_keywords,
                 time_decay, max_recoms, logger):
        super().__init__(name=name,
                         logger=logger)
        self.target_corpus = target_corpus
        self.tfidf_scheme = tfidf_scheme
        self.num_keywords = num_keywords
        self.time_decay = time_decay
        self.max_recoms = max_recoms
        self.keywords = {}
        self.recommendations = {}

    def generate_keywords(self):
        """
        For each topic in the corpus, generate using TFIDF a list of
        n_keywords most importance keywords in the form of token-to-weight
        mapping
        :param n_keywords: number of keywords to store for each topic
        """
        corpus_bow = [self.dictionary.doc2bow(data['body'])
                      for data in self.data.values()]
        tfidf = tfidfmodel.TfidfModel(corpus_bow, smartirs=self.tfidf_scheme)

        for tid, data in self.data.items():
            weights = tfidf[self.dictionary.doc2bow(data['body'])]
            weights.sort(key=lambda x: x[1], reverse=True)
            # generate token-to-weight mapping instead of id-to-weight mapping
            self.keywords[tid] = {self.dictionary[wid]: weight
                                  for wid, weight in weights[:self.num_keywords]}

    def update_on_new_topic(self, topic_id, content, date):
        relevance = 0
        date = datetime.fromtimestamp(date)
        for tid, kw in self.keywords.items():
            recom = self.recommendations[tid]
            dt = datetime.fromtimestamp(self.data[tid]['date'])
            for word in content:
                relevance += kw[word] if word in kw else 0
            relevance *= min(1, math.pow(self.time_decay, (dt - date).days))
            del_id = insert(recom, topic_id, relevance, self.max_recoms)
            if del_id is not None:
                self.data[tid]['updated'] = True

    def add(self, topic_id, content, date):
        if not super().add(topic_id, content, date):
            return False

        self.generate_keywords()
        relevance = 0
        for tid, data in self.target_corpus.data.items():
            dt = datetime.fromtimestamp(data['date'])
            for word in data['body']:
                relevance += self.keywords[topic_id].get(word, 0)
            relevance *= min(1, math.pow(self.time_decay, (date - dt).days))
            del_id = insert(self.recommendations[topic_id], tid, relevance, self.max_recoms)


    def save(self, save_dir):
        '''
        Saves the corpus and similarity data to disk
        Args:
        save_dir: directory under which to save the data
        mod_num:  number of data folders
        '''
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        for tid, data in self.data.items():
            if data['updated']:
                record = {'date': data['date'],
                          'body': data['body'],
                          'recoms': self.recommendations[tid]}
                with open(os.path.join(save_dir, tid), 'w') as f:
                    json.dump(record, f)
                data['updated'] = False
                self.logger.info('data for special topic %s updated on disk', tid)
            else:
                self.logger.info('No updates for special topic %s', tid)


class CorpusSimilarity(Corpus):
    '''
    Corpus collection
    '''
    def __init__(self, name, time_decay, duplicate_thresh,
                 irrelevant_thresh, max_recoms, logger):
        super().__init__(name=name,
                         logger=logger)
        self.time_decay = time_decay
        self.duplicate_thresh = duplicate_thresh
        self.irrelevant_thresh = irrelevant_thresh
        self.max_recoms = max_recoms

    def load(self, save_dir):       
        super().load(save_dir)
        
        folders = os.listdir(save_dir)
        for folder in folders:
            if not folder.isnumeric():
                continue
            files = os.listdir(os.path.join(save_dir, folder))
            for file in files:
                if not file.isnumeric():
                    continue
                path = os.path.join(save_dir, folder, file)
                try:
                    with open(path, 'r') as f:
                        rec = json.load(f)
                        self.data[file]['sim_list'] = rec['sim_list']
                except json.JSONDecodeError:
                    self.logger.error('Failed to load similarity data for topic %s', file)

    def add(self, topic_id, content, date):
        if not super().add(topic_id, content, date):
            return False

        self.data[topic_id]['sim_list'] = []
        self.data[topic_id]['appears_in'] = []

        new_date = datetime.fromtimestamp(self.data[topic_id]['date'])
        
        bow = self.dictionary.doc2bow(self.data[topic_id]['body'])

        for tid, data in self.data.items():
            date = datetime.fromtimestamp(data['date'])
            time_factor = math.pow(self.time_decay, (new_date-date).days)
            if tid != topic_id:
                bow1 = self.dictionary.doc2bow(data['body'])
                sim = matutils.cossim(bow, bow1)
                sim_1 = sim * min(1, 1/time_factor)
                sim_2 = sim * min(1, time_factor)
                
                if self.irrelevant_thresh <= sim_1 <= self.duplicate_thresh:
                    del_id = insert(data['sim_list'], topic_id, sim_1, self.max_recoms)
                    if del_id is not None:
                        self.data[topic_id]['appears_in'].append(tid)
                        self.data[tid]['updated'] = True
                        if del_id >= 0:
                            self.data[del_id]['appears_in'].remove(tid)
                
                if self.irrelevant_thresh <= sim_2 <= self.duplicate_thresh:
                    inserted, del_id = insert(self.data[topic_id]['sim_list'],
                                              tid, sim_2, self.max_recoms)
                    if del_id is not None:
                        self.data[tid]['appears_in'].append(topic_id)
                        if del_id >= 0:
                            self.data[del_id]['appears_in'].remove(topic_id)

        self.logger.info('Topic %s has been added to similarity results', topic_id)

        return True

    def delete(self, topic_id):
        print('deleting {}'.format(topic_id))

        if topic_id not in self.data:
            return

        appears_in = self.data[topic_id]['appears_in']

        for tid in appears_in[topic_id]: # list of topic id's whose similarity lists tid appears in
            if tid in self.data:
                self.data[tid]['sim_list'] = [x for x in self.data[tid]['sim_list']
                                              if x[0] != topic_id]
                self.data[tid]['updated'] = True

        del self.data[topic_id]
        self.logger.info('Topic %s has been deleted from similarity results', topic_id)

    def remove_before(self, t):
        if type(t) != float:
            t = time.mktime(t.timetuple())

        for tid in list(self.data.keys()):
            if self.data[tid]['date'] < t:
                self.delete(tid)

    def find_most_similar(self, topic):
        '''
        Given a topic, compute its similarities with all topics 
        in the corpus and return the top n most similar ones from 
        the corpus
        '''
        sim_list = []
        bow = self.dictionary.doc2bow(topic['body'])

        for tid, data in self.data.items():
            bow1 = self.dictionary.doc2bow(data['body'])
            sim = matutils.cossim(bow, bow1)
            if self.irrelevant_thresh <= sim <= self.duplicate_thresh:
                insert(sim_list, tid, sim, self.max_recoms)

        return sim_list

    def save(self, save_dir, mod_num):
        '''
        Saves the corpus and similarity data to disk
        Args:
        save_dir: directory under which to save the data
        mod_num:  number of data folders
        ''' 
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        for tid, data in self.data.items():
            if data['updated']:
                record = {'date': data['date'],
                          'body': data['body'],
                          'sim_list': data['sim_list']}
                folder_name = str(int(tid) % mod_num)
                dir_path = os.path.join(save_dir, folder_name)
                # build the subdir for storing topics
                if not os.path.exists(dir_path):
                    os.makedirs(dir_path) 
                filename = os.path.join(dir_path, tid)
                with open(filename, 'w') as f:
                    json.dump(record, f)
                data['updated'] = False
                self.logger.info('similarity data for topic %s updated on disk', tid)
            else:
                self.logger.info('No updates for topic %s', tid)
