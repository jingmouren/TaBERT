import json
import multiprocessing
import os, sys
import time
import traceback
from argparse import ArgumentParser, Namespace
from multiprocessing import connection
from types import SimpleNamespace
from typing import List, Dict

import gc
import zmq
from pymemcache.client.base import Client as MemcachedClient

import numpy as np

import shelve
from pathlib import Path
from tempfile import TemporaryDirectory
from tqdm import tqdm, trange

from random import shuffle, choice, sample, random

from pytorch_pretrained_bert import *

from model.dataset import Example


TRAIN_INSTANCE_QUEUE_ADDRESS = 'tcp://127.0.0.1:15566'
EXAMPLE_QUEUE_ADDRESS = 'tcp://127.0.0.1:15567'
MEMCACHED_SERVER_ADDR = 'localhost'


class TableDatabase:
    def __init__(self):
        self.client = MemcachedClient((MEMCACHED_SERVER_ADDR, 11211))
        self.client.flush_all()

        self._cur_index = multiprocessing.Value('i', 0)

    @staticmethod
    def __load_process_zmq(file, num_workers):
        context = zmq.Context()
        job_sender = context.socket(zmq.PUSH)
        job_sender.setsockopt(zmq.LINGER, -1)
        job_sender.bind("tcp://127.0.0.1:5557")

        cnt = 0
        with file.open() as f:
            for line in f:
                job_sender.send_string(line)
                # if cnt % 10000 == 0:
                #     print(f'read {cnt} examples')
                #     sys.stdout.flush()

        while True:
            job_sender.send_string('')
            time.sleep(0.1)

    @staticmethod
    def __example_worker_process_zmq(tokenizer, db, worker_status_queue):
        context = zmq.Context()
        job_receiver = context.socket(zmq.PULL)
        job_receiver.setsockopt(zmq.LINGER, -1)
        job_receiver.connect("tcp://127.0.0.1:5557")

        cache_client = MemcachedClient((MEMCACHED_SERVER_ADDR, 11211))
        buffer_size = 20000

        def _add_to_cache():
            if buffer:
                with db._cur_index.get_lock():
                    index_end = db._cur_index.value + len(buffer)
                    db._cur_index.value = index_end
                index_start = index_end - len(buffer)
                values = {str(i): val for i, val in zip(range(index_start, index_end), buffer)}
                cache_client.set_many(values, noreply=False)
                del buffer[:]

        cnt = 0
        buffer = []
        while True:
            job = job_receiver.recv_string()
            if job:
                cnt += 1
                example = Example.from_dict(json.loads(job), tokenizer, suffix=None)

                # TODO: move this to data pre-processing
                if any(len(col.name.split(' ')) > 10 for col in example.header):
                    continue

                data = example.serialize()
                buffer.append(json.dumps(data))

                if len(buffer) >= buffer_size:
                    _add_to_cache()

                if cnt % 10000 == 0:
                    worker_status_queue.put('HEART_BEAT')
            else:
                job_receiver.close()
                worker_status_queue.put('EXIT')
                break

            cnt += 1
            # if cnt % 1000 == 0:
            #     print(f'[__example_worker_process] read {cnt} examples')
            #     sys.stdout.flush()

        _add_to_cache()

    @classmethod
    def from_jsonl(cls, file_path: Path, tokenizer: BertTokenizer) -> 'TableDatabase':
        file_path = Path(file_path)
        db = cls()
        num_workers = multiprocessing.cpu_count() - 5
        worker_status_queue = multiprocessing.Queue()

        reader = multiprocessing.Process(target=cls.__load_process_zmq, args=(file_path, num_workers),
                                         daemon=True)

        workers = []
        for _ in range(num_workers):
            worker = multiprocessing.Process(target=cls.__example_worker_process_zmq,
                                             args=(tokenizer, db, worker_status_queue),
                                             daemon=True)
            worker.start()
            workers.append(worker)

        reader.start()

        stop_count = 0
        db_size = 0
        with tqdm(desc="Loading Dataset", unit=" entries", file=sys.stdout) as pbar:
            while True:
                worker_status = worker_status_queue.get()
                if worker_status == 'HEART_BEAT':
                    cur_db_size = len(db)
                    pbar.update(cur_db_size - db_size)
                    db_size = cur_db_size
                elif worker_status == 'EXIT':
                    stop_count += 1
                    print(f'{stop_count} worker stoped!')
                    if stop_count == num_workers:
                        break
                    else:
                        continue

        for worker in workers:
            worker.join()
        reader.terminate()

        return db

    def __len__(self):
        return self._cur_index.value

    def __getitem__(self, item):
        result = self.client.get(str(item))
        if result is None:
            raise IndexError(item)

        example = Example.from_serialized(json.loads(result))

        return example

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, traceback):
        self.client.close()


def create_training_instances_from_example(example: Example,
                                           masked_context_token_prob: float, mask_column_token_prob: float,
                                           max_context_length: int,
                                           max_sequence_length: int,
                                           max_predictions_per_seq: int,
                                           column_delimiter: str,
                                           tokenizer: BertTokenizer,
                                           vocab_list: list) -> List[Dict]:
    # Account for [CLS], [SEP], [SEP]

    context_before, context_after = example.context[0], example.context[1]
    if not context_before:
        context = context_after[::-1]
    elif not context_after:
        context = context_before
    elif random() < 0.5:
        context = context_after[::-1]
    else:
        context = context_before

    selected_context = []
    for i in reversed(range(0, len(context))):
        sent = context[i]
        selected_context = sent + selected_context

        if len(selected_context) > max_context_length:
            selected_context = selected_context[-max_context_length:]  # only keep context close to the table

    assert len(selected_context) > 0

    tokens_a = ['[CLS]'] + selected_context + ['[SEP]']
    # segment_ids = [0] * len(sequence)
    context_cand_indices = list(range(1, len(tokens_a) - 1))

    tokens_b = []
    column_cand_indices = []

    max_table_token_length = max_sequence_length - len(tokens_a) - 1  # account for ending [SEP]
    col_start_idx = len(tokens_a)
    for col_id, column in enumerate(example.header):
        col_tokens = list(column.name_tokens)
        col_name_indices = list(range(col_start_idx, col_start_idx + len(column.name_tokens)))

        col_tokens += ['('] + [column.type] + [')']
        col_type_idx = col_start_idx + len(column.name_tokens) + 1

        col_values = example.column_data[col_id]
        # print(col_values)
        col_values = [val for val in col_values if val is not None and len(val) > 0]
        sampled_value = choice(col_values)
        # print('chosen value', sampled_value)
        sampled_value_tokens = tokenizer.tokenize(sampled_value)

        col_tokens += ['('] + sampled_value_tokens[:5] + [')']
        col_tokens += [column_delimiter]

        _col_cand_indices = col_name_indices + [col_type_idx]

        tokens_b += col_tokens
        column_cand_indices.extend(_col_cand_indices)

        if len(tokens_b) >= max_table_token_length:
            tokens_b = tokens_b[:max_table_token_length]
            column_cand_indices = [idx for idx in column_cand_indices if idx < max_sequence_length - 1]

            break

        col_start_idx += len(col_tokens)

    del tokens_b[-1]  # remove last delimiter
    sequence = tokens_a + tokens_b + ['[SEP]']
    segment_ids = [0] * len(tokens_a) + [1] * len(tokens_b) + [1]

    masked_sequence, masked_lm_positions, masked_lm_labels = create_masked_lm_predictions(sequence, context_cand_indices, column_cand_indices,
                                                                                          masked_context_token_prob, mask_column_token_prob,
                                                                                          max_predictions_per_seq, vocab_list)

    instance = {
        "tokens": masked_sequence,
        "segment_ids": segment_ids,
        "masked_lm_positions": masked_lm_positions,
        "masked_lm_labels": masked_lm_labels,
        "source": example.source
    }

    return [instance]


def create_masked_lm_predictions(tokens, context_indices, column_indices,
                                 masked_context_token_prob, mask_column_token_prob,
                                 max_predictions_per_seq, vocab_list):
    """Creates the predictions for the masked LM objective. This is mostly copied from the Google BERT repo, but
    with several refactors to clean it up and remove a lot of unnecessary variables."""

    # mask `mask_column_token_prob` of tokens in columns
    # mask `masked_lm_prob` of tokens in NL context

    num_column_tokens_to_mask = min(max_predictions_per_seq,
                                    max(2, int(round(len(column_indices) * mask_column_token_prob))))
    max_context_token_to_mask = max_predictions_per_seq - num_column_tokens_to_mask
    num_context_tokens_to_mask = min(max_context_token_to_mask,
                                     max(1, int(round(len(context_indices) * masked_context_token_prob))))

    shuffle(column_indices)
    masked_column_token_indices = sorted(sample(column_indices, num_column_tokens_to_mask))

    if num_context_tokens_to_mask:
        shuffle(context_indices)
        masked_context_token_indices = sorted(sample(context_indices, num_context_tokens_to_mask))
        masked_indices = sorted(masked_context_token_indices + masked_column_token_indices)
    else:
        masked_indices = masked_column_token_indices

    masked_token_labels = []

    for index in masked_indices:
        # 80% of the time, replace with [MASK]
        if random() < 0.8:
            masked_token = "[MASK]"
        else:
            # 10% of the time, keep original
            if random() < 0.5:
                masked_token = tokens[index]
            # 10% of the time, replace with random word
            else:
                masked_token = choice(vocab_list)
        masked_token_labels.append(tokens[index])
        # Once we've saved the true label for that token, we can overwrite it with the masked version
        tokens[index] = masked_token

    return tokens, masked_indices, masked_token_labels


def generate_train_instance_from_example(table_db: TableDatabase, indices: List[int], status_queue: multiprocessing.Queue, args: Namespace):
    context = zmq.Context()
    instance_sender = context.socket(zmq.PUSH)
    # instance_sender.setsockopt(zmq.LINGER, -1)
    instance_sender.connect(TRAIN_INSTANCE_QUEUE_ADDRESS)

    table_db.client = MemcachedClient((MEMCACHED_SERVER_ADDR, 11211))

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)
    vocab_list = list(tokenizer.vocab.keys())

    # print('started queues')
    num_processed = 0
    for idx in indices:
            example = table_db[idx]
            # print('get one example')
            try:
                instances = create_training_instances_from_example(
                    example,
                    max_context_length=args.max_context_len,
                    max_sequence_length=args.max_seq_len,
                    max_predictions_per_seq=args.max_predictions_per_seq,
                    masked_context_token_prob=args.masked_context_prob,
                    mask_column_token_prob=args.masked_column_prob,
                    column_delimiter=args.column_delimiter,
                    tokenizer=tokenizer,
                    vocab_list=vocab_list
                )
                for instance in instances:
                    instance_sender.send_pyobj(json.dumps(instance))

                num_processed += 1
                if num_processed == 1000:
                    status_queue.put(('HEART_BEAT', num_processed))
                    num_processed = 0
            except:
                typ, value, tb = sys.exc_info()
                print('*' * 50 + 'Exception' + '*' * 50, file=sys.stderr)
                print(example.serialize(), file=sys.stderr)
                print('*' * 50 + 'Stack Trace' + '*' * 50, file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                print('*' * 50 + 'Exception' + '*' * 50, file=sys.stderr)

                sys.stderr.flush()

    instance_sender.send_pyobj(None)
    status_queue.put('EXIT')

    while True:
        time.sleep(10)


def write_instance_to_file(output_file: Path, num_workers: int, stat_send: connection.Connection):
    context = zmq.Context()
    instance_receiver = context.socket(zmq.PULL)
    # instance_receiver.setsockopt(zmq.LINGER, -1)
    instance_receiver.bind(TRAIN_INSTANCE_QUEUE_ADDRESS)

    finished_worker_num = 0
    num_instances = 0
    with output_file.open('w') as f:
        while True:
            data = instance_receiver.recv_pyobj()
            if data is not None:
                f.write(data + os.linesep)
                num_instances += 1
                # print('write one example')
            else:
                # print('one worker finished')
                finished_worker_num += 1
                if finished_worker_num == num_workers:
                    break

    stat_send.send(num_instances)
    instance_receiver.close()


def generate_for_epoch(table_db: TableDatabase,
                       indices: List[int],
                       epoch_file: Path,
                       metrics_file: Path,
                       args: Namespace):
    print(f'Generating {epoch_file}', file=sys.stderr)

    stat_recv, stat_send = multiprocessing.Pipe()
    num_workers = multiprocessing.cpu_count() - 2

    instance_writer_process = multiprocessing.Process(target=write_instance_to_file,
                                                      args=(epoch_file, num_workers, stat_send))
    instance_writer_process.start()

    workers = []
    worker_status_queue = multiprocessing.Queue()
    for i in range(num_workers):
        indices_chunk = indices[i: len(indices): num_workers]
        worker_process = multiprocessing.Process(target=generate_train_instance_from_example,
                                                 args=(table_db, indices_chunk, worker_status_queue, args),
                                                 daemon=True)
        worker_process.start()
        workers.append(worker_process)

    finished_worker_num = 0
    with tqdm(desc="Document", file=sys.stdout) as pbar:
        while True:
            status = worker_status_queue.get()
            if status == 'EXIT':
                finished_worker_num += 1
                if finished_worker_num == num_workers:
                    break
            elif status[0] == 'HEART_BEAT':
                num_processed = status[1]
                pbar.update(num_processed)

    num_instances = stat_recv.recv()
    print('num instanances:', num_instances)
    instance_writer_process.join()

    for worker in workers:
        worker.terminate()

    with metrics_file.open('w') as f:
        metrics = {
            "num_training_examples": num_instances,
            "max_seq_len": args.max_seq_len
        }
        f.write(json.dumps(metrics))


def main():
    parser = ArgumentParser()
    parser.add_argument('--train_corpus', type=Path, required=True)
    parser.add_argument("--output_dir", type=Path, required=True)
    parser.add_argument("--bert_model", type=str, required=True,
                        choices=["bert-base-uncased", "bert-large-uncased", "bert-base-cased",
                                 "bert-base-multilingual", "bert-base-chinese"])
    parser.add_argument("--do_lower_case", action="store_true")

    parser.add_argument("--reduce_memory", action="store_true",
                        help="Reduce memory usage for large datasets by keeping data on disc rather than in memory")

    parser.add_argument("--epochs_to_generate", type=int, default=3,
                        help="Number of epochs of data to pregenerate")
    parser.add_argument("--max_seq_len", type=int, default=512)
    parser.add_argument("--max_context_len", type=int, default=256)
    parser.add_argument("--masked_context_prob", type=float, default=0.15,
                        help="Probability of masking each token for the LM task")
    parser.add_argument("--masked_column_prob", type=float, default=0.20,
                        help="Probability of masking each token for the LM task")
    parser.add_argument("--max_predictions_per_seq", type=int, default=20,
                        help="Maximum number of tokens to mask in each sequence")
    parser.add_argument("--column_delimiter", type=str, default='[SEP]', help='Column delimiter')

    parser.add_argument('--no_wiki_tables_from_common_crawl', action='store_true', default=False)

    args = parser.parse_args()

    global tokenizer
    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)

    with TableDatabase.from_jsonl(args.train_corpus, tokenizer=tokenizer) as table_db:
        args.output_dir.mkdir(exist_ok=True, parents=True)
        print(f'Num entries in database: {len(table_db)}', file=sys.stderr)

        # generate train and dev split
        example_indices = list(range(len(table_db)))
        shuffle(example_indices)
        dev_size = min(int(len(table_db) * 0.1), 500000)
        train_indices = example_indices[:-dev_size]
        dev_indices = example_indices[-dev_size:]

        (args.output_dir / 'train').mkdir(exist_ok=True)
        (args.output_dir / 'dev').mkdir(exist_ok=True)

        # generate dev data first
        dev_file = args.output_dir / 'dev' / 'epoch_0.json'
        dev_metrics_file = args.output_dir / 'dev' / "epoch_0_metrics.json"
        generate_for_epoch(table_db, dev_indices, dev_file, dev_metrics_file, args)

        for epoch in trange(args.epochs_to_generate, desc='Epoch'):
            gc.collect()
            epoch_filename = args.output_dir / 'train' / f"epoch_{epoch}.json"
            metrics_file = args.output_dir / 'train' / f"epoch_{epoch}_metrics.json"
            generate_for_epoch(table_db, train_indices, epoch_filename, metrics_file, args)


if __name__ == '__main__':
    main()
