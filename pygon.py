#!/usr/bin/env python3
# coding: utf-8

import calendar
import concurrent.futures
import json
import os
import shutil
import sys
import tempfile
import warnings
from collections import defaultdict, namedtuple
from pathlib import Path
from typing import NamedTuple, Type, NoReturn, Union

import dill
import grip
import newspaper
import pandas as pd
from pygooglenews import GoogleNews
from rich.console import Console


class NoEntriesError(Exception):
    pass


class Count:
    count = None


class Search:
    def __init__(self, **kwargs) -> None:
        self.query = kwargs.pop('query')
        self.month = kwargs.pop('month')
        self.year = kwargs.pop('year')
        self.language = kwargs.pop('language').lower()
        self.country = kwargs.pop('country').upper()
        self.testing = kwargs.pop('testing')
        self.silent = kwargs.pop('silent')

    def create_date(self) -> int:
        """
        Converts self.month and self.year to a zero-padded decimal number
        string, and checks the number of days in self.month
        :return: the number of days in a month
        """
        if int(self.month) <= 9:
            self.month = f'0{self.month}'
        if int(self.month) in [1, 3, 5, 7, 8, 10, 12]:
            last_day = 31
        elif int(self.month) == 2:
            if calendar.isleap(self.year):
                last_day = 29
            else:
                last_day = 28
        else:
            last_day = 30
        return last_day

    def request(self) -> dict:
        """
        Searches for news articles using Google News API. If self.month has
        more than 100 entries, a separate request for each day of the month is
        sent (will only retrieve the first 100 entries if there is > 100
        entry in a given day)
        :return: dictionary with metadata of the news articles
        """
        console = Console()
        gn = GoogleNews(lang=self.language, country=self.country)

        last_day = Search.create_date(self)
        from_ = f'{self.year}-{self.month}-01'
        to_ = f'{self.year}-{self.month}-{last_day}'
        month_name = calendar.month_name[int(self.month)]
        console.rule(f'{month_name}, {self.year}')

        res = gn.search(self.query, from_=from_, to_=to_)
        count = len(res['entries'])
        if count == 0:
            Count.count = 0
        if count == 100:
            console.print(f'Found +{count} articles')
        else:
            console.print(f'Found {count} articles')
        if count >= 100:
            res['entries'].clear()
            for day in range(1, last_day):
                if day <= 9:
                    if day != 9:
                        next_day = f'0{day + 1}'
                    day = f'0{day}'
                else:
                    next_day = day + 1
                res_1 = gn.search(self.query,
                                  from_=f'{self.year}-{self.month}-{day}',
                                  to_=f'{self.year}-{self.month}-'
                                  f'{next_day}')  # noqa
                res['entries'].extend(res_1['entries'])
        return res

    def improve_results(self, raw_data: dict) -> dict:
        """
        Removes redundant and unnecessary data from the API response, and uses
        `Newspaper3k` to summarize the article and extract keywords.
        :param raw_data: data dictionary retrieved from Search.request
        :return: dictionary of the clean/improved data
        """
        def iterate_over_articles(article):
            """
            Inner function to be used later in a concurrent.futures loop to
            execute calls asynchronously
            :param article: a single raw dict item
            :return: a clean and improved dict of the raw article dict
            """
            exclude_keys = [
                'title_detail', 'links', 'summary_detail', 'guidislink',
                'sub_articles', 'published_parsed', 'summary'
            ]
            article = {
                k: v
                for k, v in article.items() if k not in exclude_keys
            }

            article_obj = newspaper.Article(article['link'],
                                            language=self.language)
            try:
                article_obj.download()
                article_obj.parse()
                article_obj.nlp()
                article['keywords'] = article_obj.keywords
                article['summary'] = article_obj.summary
            except newspaper.article.ArticleException:
                if not self.silent:
                    print(f'Skipped nlp for {article["title"]}...')
                article['keywords'] = []
                article['summary'] = ''
            return article

        output_dict = defaultdict(dict)
        output_dict['feed'] = raw_data['feed']
        output_dict['results']['entries'] = entries = []

        with concurrent.futures.ThreadPoolExecutor() as executor:
            results = [
                executor.submit(iterate_over_articles, article)
                for article in raw_data['entries']
            ]
            for future in concurrent.futures.as_completed(results):
                entries.append(future.result())
        return output_dict

    def filename(self) -> str:
        """
        Process self attributes to return a file name based on the month and
        year, if self.testing is "True," then the query is added to the file
        name.
        :return: a string representing the file name
        """
        Search.create_date(self)
        if self.testing:
            fname = f'{self.query}__results_{self.year}_{self.month}'
        else:
            fname = f'results_{self.year}_{self.month}'
        return fname

    def mkdir_ifnot(self, subdir: str, gh_pages: bool = False) -> str:
        """
        Creates a directory if the data directory does not exists
        :param subdir: the data type: "json", "json/raw", "html", "pickle",
        or "excel".
        :param gh_pages: whether the output should be directly exported to
        the github pages website directory
        :return: the path to the directory (and its parents, if applicable).
        """
        path = f'data/{self.year}/{subdir}/{self.language.upper()}'
        if gh_pages:
            path = f'docs/{self.year}/{self.language.upper()}'
        Path(path).mkdir(parents=True, exist_ok=True)
        return path

    def run(self) -> Type[NamedTuple]:
        """
        The main function of the class.
        :return: a namedtuple with two dictionaries: raw (original API
        response) and improved (without redundant/useless data, with a
        summary and keywords for each article
        """
        Data = namedtuple('Data', ['raw', 'improved'])
        Data.raw = Search.request(self)
        Data.improved = Search.improve_results(self, Data.raw)
        return Data


class _CheckEmpty:
    def __init__(self, data: Type[NamedTuple]) -> None:
        self.data = data

    def return_data(self) -> Union[Type[NamedTuple], NoReturn]:
        if not self.data.raw['entries']:
            raise NoEntriesError(
                'Cannot export because no articles were found.')
        return self.data


class ExportData(Search):
    def __init__(self, data: Type[NamedTuple], **kwargs) -> None:
        super().__init__(**kwargs)
        self.data = _CheckEmpty(data).return_data()
        self.fname = Search.filename(self)

    def _to_pandas(self) -> pd.DataFrame:
        df = pd.DataFrame.from_dict(self.data.improved['results']['entries'])
        df.columns = df.columns.str.capitalize()
        df['Published'] = pd.to_datetime(df.Published).dt.date
        df.sort_values('Published', inplace=True)
        df.replace(r'\\n', ' ', regex=True, inplace=True)
        df.reset_index(inplace=True)
        df.pop('index')
        return df

    def to_excel(self) -> None:
        path = Search.mkdir_ifnot(self, 'excel')
        df = ExportData._to_pandas(self)
        df.to_excel(f'{path}/{self.fname}.xlsx', encoding='utf-8-sig')

    def to_json(self) -> None:
        path_raw = Search.mkdir_ifnot(self, 'json/raw')
        path = Search.mkdir_ifnot(self, 'json')
        with open(f'{path_raw}/raw_{self.fname}.json', 'w') as j:
            json.dump(self.data.raw, j, indent=4)
        with open(f'{path}/{self.fname}.json', 'w') as j:
            json.dump(self.data.improved, j, indent=4, ensure_ascii=False)

    def to_pickle(self) -> None:
        path = Search.mkdir_ifnot(self, 'pickle')
        d = self.data(self.data.raw, self.data.improved)
        with open(f'{path}/{self.fname}.pkl', 'wb') as pkl:
            dill.dump(d, pkl)

    def to_html(self, keep_md: bool = False, to_ghpages: bool = False) -> list:
        def remove_bad_chars(x: str) -> str:
            return x.replace('|', ' ').replace('\n', ' ').replace('  ', ' ')

        def md_link(link: str) -> str:
            return f'[Link]({link})'

        def source(d: dict) -> str:
            stitle = ''.join(
                [' ' if x in list('()[]|') else x for x in d['title']])
            slink = d["href"]
            return f'[{stitle}]({slink})'

        df = ExportData._to_pandas(self)
        df['Summary'] = df.Summary.apply(remove_bad_chars)
        df['Title'] = df.Title.apply(remove_bad_chars)
        df['Link'] = df.Link.apply(md_link)
        df['Source'] = df.Source.apply(source)
        df.pop('Id')
        md = df.to_markdown()
        path = Search.mkdir_ifnot(self, 'html')
        html_path = f'{path}/{self.fname}.html'
        new_name = f'{self.month}_{self.year} - {self.query}'
        with tempfile.NamedTemporaryFile() as fp:
            fp.write(md.encode('utf-8'))
            fp.seek(0)
            fp.read().decode('utf-8')
            if keep_md:
                path_md = Search.mkdir_ifnot(self, 'md')
                os.link(fp.name, f'{path_md}/{self.fname}.md')
            warnings.simplefilter("ignore", ResourceWarning)
            grip.export(path=fp.name,
                        out_filename=html_path,
                        title=new_name,
                        quiet=True)
        stdout = sys.stdout
        with open(os.devnull, 'w') as f:
            sys.stdout = f
            grip.clear_cache()
        sys.stdout = stdout
        with open(html_path, 'r+') as f:
            lines = f.readlines()
        if to_ghpages:
            gh_path = Search.mkdir_ifnot(self, '', gh_pages=True)
            shutil.copy2(html_path, gh_path)
        return lines