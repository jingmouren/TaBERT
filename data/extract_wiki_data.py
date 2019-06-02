import fileinput
import os, sys
import html
import traceback
from argparse import ArgumentParser
from pathlib import Path
from typing import Dict, Optional, Iterator
import multiprocessing

import spacy
from tqdm import tqdm
import wikitextparser as wtq
from data.WikiExtractor import pages_from, options, tagRE, Extractor, ignoreTag
from data.webtable import *

# os.environ['JAVA_HOME'] = '/Library/Java/JavaVirtualMachines/jdk-12.0.1.jdk/Contents/Home'
# os.environ['CLASSPATH'] = '/Users/pengcheng/Projects/tableBERT/target/tableBERT-1.0-SNAPSHOT-jar-with-dependencies.jar'

_extractor = Extractor('', '', '', [])


def wiki2text(wiki_text):
    text = _extractor.transform(wiki_text)
    text = _extractor.wiki2text(text)
    text = _extractor.clean(text)

    return text


def is_valid_table(table: WebTable) -> bool:
    # check the size of the table
    if table.num_cols < 3 or table.num_rows < 4:
        return False

    return True


class TableExtractor(multiprocessing.Process):
    def __init__(self, job_queue: multiprocessing.Queue,
                 example_queue: multiprocessing.Queue,
                 **kwargs):
        super(TableExtractor, self).__init__(**kwargs)

        self.job_queue = job_queue
        self.example_queue = example_queue

    def run(self):
        from jnius import autoclass
        self.mediaWikiToHtml = autoclass('MediaWikiToHtml')
        print('finished loading Java class')

        self.nlp = spacy.load('en_core_web_sm')
        print('loaded NLP model')

        job = self.job_queue.get()
        while job is not None:
            id, revid, title, ns, catSet, page = job
            page_content = ''.join(page)

            try:
                for example in self.extract(id, title, page_content):
                    # print(example)
                    self.example_queue.put(example)
            except:
                typ, value, tb = sys.exc_info()
                print('*' * 30 + 'Exception' + '*' * 30, file=sys.stderr)
                print(f'id={id}, title={title}', file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

            job = self.job_queue.get()

    def extract(self, id, title, page_content) -> Iterator[Dict]:
        page_context = html.unescape(page_content)
        wiki_page = wtq.parse(page_context)

        for table in wiki_page.tables:
            # print(f'[WikiTitle]{title}')
            # try:
            try:
                table_data = table.data()
            except:
                continue

            if len(table_data) == 0:
                continue

            col_num = len(table_data[0])
            row_num = len(table_data)

            if col_num < 3 or row_num < 4:
                continue

            # caption = table.caption
            # caption_from_first_row = False
            # if not caption:
            #     first_cell = table.cells(0, 0)
            #     if str(first_cell.get_attr('colspan')) == str(col_num):
            #         caption = wiki2text(str(first_cell))
            #         caption_from_first_row = True
            #
            # if caption:
            #     caption = wiki2text(caption)

            tab_span = table.span
            context_end = tab_span[0]
            context = wiki_page[: context_end]
            cleaned_ctx = wiki2text(context)
            cleaned_ctx = [x for x in cleaned_ctx.strip().split('\n') if x][-3:]
            if any('|' in x for x in cleaned_ctx) or any('{' in x for x in cleaned_ctx):
                continue

            parsed_context = []
            for paragraph in cleaned_ctx:
                paragraph_sents = []
                parsed_paragraph = self.nlp(paragraph)
                for sent in parsed_paragraph.sents:
                    paragraph_sents.append(sent.text)
                parsed_context.append(paragraph_sents)

            table_html = self.mediaWikiToHtml.convert(str(table))
            table = self.extract_table_data(table_html)

            # if there is not any context
            if table and not parsed_context and not table.caption:
                continue

            if table:
                table = table.to_dict()
                example = {
                    'id': id,
                    'title': title,
                    'context': parsed_context
                }

                example.update(table)

                yield example
        # except:
        #    continue
        # if wiki_page.tables:
        #     table_count += len(wiki_page.tables)

    def extract_table_data(self, table_html: str) -> Optional[WebTable]:
        soup_doc = BeautifulSoup(table_html).find(class_='wikitable') # type: BeautifulSoupOriginal
        if soup_doc is None:
            return None

        # ignore nested table
        if soup_doc.find('table'):
            return None

        table = WebTable(soup_doc,
                         normalization=WebTable.NORM_DUPLICATE,
                         first_row_as_caption=True)

        table.clean()
        if not is_valid_table(table):
            return None

        table.annotate_schema(nlp_model=self.nlp)

        return table


def get_pages_from_data_dump(input_file):
    ignoredTags = [
        'abbr', 'b', 'big', 'blockquote', 'center', 'cite', 'em',
        'font', 'h1', 'h2', 'h3', 'h4', 'hiero', 'i', 'kbd',
        'p', 'plaintext', 's', 'span', 'strike', 'strong',
        'tt', 'u', 'var'
    ]

    # 'a' tag is handled separately
    for tag in ignoredTags:
        ignoreTag(tag)

    input = fileinput.FileInput(str(input_file.resolve()), openhook=fileinput.hook_compressed)

    # skip site info
    for line in input:
        # When an input file is .bz2 or .gz, line can be a bytes even in Python 3.
        if not isinstance(line, str): line = line.decode('utf-8')
        m = tagRE.search(line)
        if not m:
            continue
        tag = m.group(2)
        if tag == '/siteinfo':
            break

    page_iter = pages_from(input)
    for page_data in tqdm(page_iter, file=sys.stdout):
        yield page_data


def data_loader_process(input_file, job_queue, num_workers):
    page_iter = get_pages_from_data_dump(input_file)
    for data in page_iter:
        job_queue.put(data)

    for i in range(num_workers):
        job_queue.put(None)


def example_writer_process(output_file, example_queue):
    data = example_queue.get()
    with output_file.open('w') as f:
        while data is not None:
            d = json.dumps(data)
            f.write(d + os.linesep)

            data = example_queue.get()


def process():
    parser = ArgumentParser()
    parser.add_argument('--wiki_dump', type=Path, required=True)
    parser.add_argument("--output_file", type=Path, required=True)

    args = parser.parse_args()

    # input_file = '/Users/pengcheng/Downloads/enwiki-20190520-pages-articles-multistream1.xml-p10p30302.bz2'
    # output_file = 'wiki_dump.jsonl'

    job_queue = multiprocessing.Queue(maxsize=1000)
    example_queue = multiprocessing.Queue()
    num_workers = multiprocessing.cpu_count() - 1

    loader = multiprocessing.Process(target=data_loader_process, daemon=True, args=(args.wiki_dump, job_queue, num_workers))
    loader.start()

    workers = []
    for i in range(num_workers):
        worker = TableExtractor(job_queue, example_queue, daemon=True)
        worker.start()
        workers.append(worker)

    writer = multiprocessing.Process(target=example_writer_process, daemon=True, args=(args.output_file, example_queue))
    writer.start()

    for worker in workers:
        worker.join()
    loader.join()

    example_queue.put(None)
    writer.join()


if __name__ == '__main__':
    process()
