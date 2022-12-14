# -*- coding: utf-8 -*-
import json
import os
import sys
sys.path.append('..')

from deep_training.nlp.metrics.pointer import metric_for_pointer
from pytorch_lightning.utilities.types import EPOCH_OUTPUT
import typing

from deep_training.nlp.models.transformer import TransformerMeta
from pytorch_lightning.callbacks import ModelCheckpoint
from deep_training.data_helper import DataHelper
import torch
import numpy as np
from pytorch_lightning import Trainer
from deep_training.data_helper import make_dataset_with_args, load_dataset_with_args,load_tokenizer_and_config_with_args
from deep_training.nlp.models.w2ner import TransformerForW2ner,extract_lse,W2nerArguments
from transformers import HfArgumentParser, BertTokenizer
from deep_training.data_helper import ModelArguments, DataArguments, TrainingArguments

train_info_args = {
    'devices': '1',
    'data_backend': 'memory_raw',
    'model_type': 'bert',
    'model_name_or_path':'/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'tokenizer_name':'/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'config_name':'/data/nlp/pre_models/torch/bert/bert-base-chinese/config.json',
    'do_train': True,
    'do_eval': True,
    'train_file': '/data/nlp/nlp_train_data/clue/cluener/train.json',
    'eval_file': '/data/nlp/nlp_train_data/clue/cluener/dev.json',
    'test_file': '/data/nlp/nlp_train_data/clue/cluener/test.json',
    'learning_rate': 5e-5,
    'learning_rate_for_task': 5e-5,
    'max_epochs': 15,
    'train_batch_size': 40,
    'eval_batch_size': 2,
    'test_batch_size': 1,
    'adam_epsilon': 1e-8,
    'gradient_accumulation_steps': 1,
    'max_grad_norm': 1.0,
    'weight_decay': 0,
    'warmup_steps': 0,
    'output_dir': './output',
    'train_max_seq_length': 90,
    'eval_max_seq_length': 120,
    'test_max_seq_length': 120,
#w2ner param
    'use_bert_last_4_layers':False,
    'dist_emb_size': 20,
    'type_emb_size': 20,
    'lstm_hid_size': 768,
    'conv_hid_size': 96,
    'biaffine_size': 768,
    'ffnn_hid_size': 128,
    'dilation': [1,2,3],
    'emb_dropout': 0.2,
    'conv_dropout': 0.2,
    'out_dropout': 0.1,


}



class NN_DataHelper(DataHelper):
    index = -1
    eval_labels = []

    def __init__(self,*args,**kwargs):
        super(NN_DataHelper, self).__init__(*args,**kwargs)
        dis2idx = np.zeros((1000), dtype='int64')
        dis2idx[1] = 1
        dis2idx[2:] = 2
        dis2idx[4:] = 3
        dis2idx[8:] = 4
        dis2idx[16:] = 5
        dis2idx[32:] = 6
        dis2idx[64:] = 7
        dis2idx[128:] = 8
        dis2idx[256:] = 9

        self.dis2idx = dis2idx


    # 切分成开始
    def on_data_ready(self):
        self.index = -1
    # 切分词
    def on_data_process(self, data: typing.Any, user_data: tuple):
        self.index += 1

        tokenizer: BertTokenizer
        tokenizer, max_seq_length, do_lower_case, label2id, mode = user_data
        sentence, entities = data

        tokens = list(sentence) if not do_lower_case else list(sentence.lower())
        input_ids = tokenizer.convert_tokens_to_ids(tokens)
        if len(input_ids) > max_seq_length - 2:
            input_ids = input_ids[:max_seq_length - 2]
        input_ids = [tokenizer.cls_token_id] + input_ids + [tokenizer.sep_token_id]
        attention_mask = [1] * len(input_ids)

        input_ids = np.asarray(input_ids, dtype=np.int32)
        attention_mask = np.asarray(attention_mask, dtype=np.int32)
        seqlen = np.asarray(len(input_ids), dtype=np.int32)

        real_label = []
        length = len(input_ids)
        grid_labels = np.zeros((length, length), dtype=np.int32)
        pieces2word = np.zeros((length, length), dtype=bool)
        dist_inputs = np.zeros((length, length), dtype=np.int32)
        grid_mask2d = np.ones((length, length), dtype=bool)

        for i in range(seqlen - 1):
            for j in range(i + 1, i + 2):
                pieces2word[i][j] = 1
        for k in range(seqlen):
            dist_inputs[k, :] += k
            dist_inputs[:, k] -= k

        for i in range(length):
            for j in range(length):
                if dist_inputs[i, j] < 0:
                    dist_inputs[i, j] = self.dis2idx[-dist_inputs[i, j]] + 9
                else:
                    dist_inputs[i, j] = self.dis2idx[dist_inputs[i, j]]
        dist_inputs[dist_inputs == 0] = 19


        if entities is not None:
            for l,s,e in entities:
                l = label2id[l]
                real_label.append((l,s,e))
                s += 1
                e += 1
                if s < max_seq_length - 1 and e < max_seq_length - 1:
                    for i in range(e - s):
                        grid_labels[i,i+1] = 1
                    grid_labels[e,s] = l + 2

        pad_len = max_seq_length - len(input_ids)
        if pad_len > 0:
            input_ids = np.pad(input_ids, (0, pad_len), 'constant',
                               constant_values=(tokenizer.pad_token_id, tokenizer.pad_token_id))
            attention_mask = np.pad(attention_mask, (0, pad_len), 'constant', constant_values=(0, 0))

            grid_labels = np.pad(grid_labels, pad_width=((0, pad_len), (0, pad_len)))
            pieces2word = np.pad(pieces2word, pad_width=((0, pad_len), (0, pad_len)))
            dist_inputs = np.pad(dist_inputs, pad_width=((0, pad_len), (0, pad_len)))
            grid_mask2d = np.pad(grid_mask2d, pad_width=((0, pad_len), (0, pad_len)))



        d = {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'labels': grid_labels,
            'pieces2word': pieces2word,
            'dist_inputs': dist_inputs,
            'grid_mask2d': grid_mask2d,
            'seqlen': seqlen,
        }

        # if self.index < 5:
        #     print(tokens)
        #     print(input_ids[:seqlen])
        #     print(attention_mask[:seqlen])
        #     print(seqlen)

        if mode == 'eval':
            if self.index < 3:
                print(sentence, entities)
            self.eval_labels.append(real_label)
        return d

    # 读取标签
    def on_get_labels(self, files: typing.List[str]):
        labels = [
            'address', 'book', 'company', 'game', 'government', 'movie', 'name', 'organization', 'position', 'scene'
        ]
        labels = list(set(labels))
        labels = sorted(labels)
        label2id = {label: i for i, label in enumerate(labels)}
        id2label = {i: label for i, label in enumerate(labels)}
        return label2id, id2label

    # 读取文件
    def on_get_corpus(self, files: typing.List, mode: str):
        D = []
        for filename in files:
            with open(filename, mode='r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines:
                    jd = json.loads(line)
                    if not jd:
                        continue
                    # cluener 为 label，  fastlabel 为 entities
                    entities = jd.get('label', None)
                    if entities:
                        entities_label = []
                        for k, v in entities.items():
                            pts = [_ for a_ in list(v.values()) for _ in a_]
                            for pt in pts:
                                assert pt[0] <= pt[1], ValueError(line, pt)
                                entities_label.append((k, pt[0], pt[1]))
                    else:
                        entities_label = None
                    D.append((jd['text'], entities_label))
        return D

    @staticmethod
    def collate_fn(batch):
        o = {}
        for i, b in enumerate(batch):
            if i == 0:
                for k in b:
                    o[k] = [torch.tensor(b[k])]
            else:
                for k in b:
                    o[k].append(torch.tensor(b[k]))
        for k in o:
            o[k] = torch.stack(o[k])

        max_len = torch.max(o.pop('seqlen'))
        o['input_ids'] = o['input_ids'][:, :max_len]
        o['attention_mask'] = o['attention_mask'][:, :max_len]
        if 'token_type_ids' in o:
            o['token_type_ids'] = o['token_type_ids'][:, :max_len]
        o['labels'] = o['labels'][:, :max_len, :max_len]
        o['pieces2word'] = o['pieces2word'][:, :max_len, :max_len]
        o['dist_inputs'] = o['dist_inputs'][:, :max_len, :max_len]
        o['grid_mask2d'] = o['grid_mask2d'][:, :max_len, :max_len]

        return o



class MyTransformer(TransformerForW2ner, metaclass=TransformerMeta):
    def __init__(self,eval_labels,*args, **kwargs):
        super(MyTransformer, self).__init__(*args, **kwargs)
        self.model.eval_labels = eval_labels
        self.eval_labels = eval_labels

    def validation_epoch_end(self, outputs: typing.Union[EPOCH_OUTPUT, typing.List[EPOCH_OUTPUT]]) -> None:
        label2id = self.config.label2id
        preds, trues = [], []
        eval_labels = self.eval_labels
        for i, o in enumerate(outputs):
            logits,seqlens, _ = o['outputs']
            preds.extend(extract_lse([logits,seqlens]))
            bs = len(logits)
            trues.extend(eval_labels[i * bs: (i + 1) * bs])

        print(preds[:3])
        print(trues[:3])

        f1, str_report = metric_for_pointer(trues, preds, label2id)
        print(f1)
        print(str_report)
        self.log('val_f1', f1, prog_bar=True)


if __name__ == '__main__':
    parser = HfArgumentParser((ModelArguments, TrainingArguments, DataArguments,W2nerArguments))
    model_args, training_args, data_args,w2nerArguments = parser.parse_dict(train_info_args)

    dataHelper = NN_DataHelper(data_args.data_backend)
    tokenizer, config, label2id, id2label = load_tokenizer_and_config_with_args(dataHelper, model_args, training_args,data_args)

    token_fn_args_dict = {
        'train': (tokenizer, data_args.train_max_seq_length, model_args.do_lower_case, label2id, 'train'),
        'eval': (tokenizer, data_args.eval_max_seq_length, model_args.do_lower_case, label2id, 'eval'),
        'test': (tokenizer, data_args.test_max_seq_length, model_args.do_lower_case, label2id, 'test')
    }

    N = 1
    train_files, eval_files, test_files = [], [], []
    for i in range(N):
        intermediate_name = data_args.intermediate_name + '_{}'.format(i)
        if data_args.do_train:
            train_files.append(
                make_dataset_with_args(dataHelper, data_args.train_file, token_fn_args_dict['train'], data_args,
                                       intermediate_name=intermediate_name, shuffle=True, mode='train'))
        if data_args.do_eval:
            eval_files.append(
                make_dataset_with_args(dataHelper, data_args.eval_file, token_fn_args_dict['eval'], data_args,
                                       intermediate_name=intermediate_name, shuffle=False, mode='eval'))
        if data_args.do_test:
            test_files.append(
                make_dataset_with_args(dataHelper, data_args.test_file, token_fn_args_dict['test'], data_args,
                                       intermediate_name=intermediate_name, shuffle=False, mode='test'))



    dm = load_dataset_with_args(dataHelper, training_args, train_files, eval_files, test_files)
    model = MyTransformer(dataHelper.eval_labels,w2nerArguments=w2nerArguments,config=config, model_args=model_args, training_args=training_args)
    checkpoint_callback = ModelCheckpoint(monitor="val_f1", every_n_epochs=1)
    trainer = Trainer(
        callbacks=[checkpoint_callback],
         max_epochs=training_args.max_epochs,
        max_steps=training_args.max_steps,
        accelerator="gpu",
        devices=data_args.devices,
        enable_progress_bar=True,
        default_root_dir=data_args.output_dir,
        gradient_clip_val=training_args.max_grad_norm,
        accumulate_grad_batches=training_args.gradient_accumulation_steps,
        num_sanity_val_steps=0,
    )

    if data_args.do_train:
        trainer.fit(model, datamodule=dm)

    if data_args.do_eval:
        trainer.validate(model, datamodule=dm)

    if data_args.do_test:
        trainer.test(model, datamodule=dm)