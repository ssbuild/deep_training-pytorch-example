# -*- coding: utf-8 -*-
import json
import typing

import numpy as np
import torch
from deep_training.data_helper import DataHelper
from deep_training.data_helper import ModelArguments, TrainingArguments, DataArguments
from deep_training.data_helper import load_tokenizer_and_config_with_args
from deep_training.nlp.losses.loss_cosent import CoSentLoss
from deep_training.nlp.models.transformer import TransformerModel, TransformerMeta
from deep_training.utils.func import seq_pading
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint
from torch import nn
from torch.utils.data import DataLoader, IterableDataset
from transformers import HfArgumentParser, BertTokenizer

train_info_args = {
    'devices': '1',
    'data_backend':'memory_raw',
    'model_type': 'bert',
    'model_name_or_path':'/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'tokenizer_name':'/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'config_name':'/data/nlp/pre_models/torch/bert/bert-base-chinese/config.json',
    'do_train': True,
    'train_file':'/data/nlp/nlp_train_data/clue/afqmc_public/train.json',
    'eval_file':'/data/nlp/nlp_train_data/clue/afqmc_public/dev.json',
    'test_file':'/data/nlp/nlp_train_data/clue/afqmc_public/test.json',
    'optimizer': 'adamw',
    'learning_rate':5e-5,
    'max_epochs':3,
    'train_batch_size':64,
    'test_batch_size':2,
    'adam_epsilon':1e-8,
    'gradient_accumulation_steps':1,
    'max_grad_norm':1.0,
    'weight_decay':0,
    'warmup_steps':0,
    'output_dir':'./output',
    'max_seq_length':140
}


def pad_to_seqlength(sentence,tokenizer,max_seq_length):
    tokenizer: BertTokenizer
    o = tokenizer(sentence, max_length=max_seq_length, truncation=True, add_special_tokens=True, )
    arrs = [o['input_ids'],o['attention_mask']]
    seqlen = np.asarray(len(arrs[0]),dtype=np.int64)
    input_ids,attention_mask = seq_pading(arrs,max_seq_length=max_seq_length,pad_val=tokenizer.pad_token_id)
    return input_ids,attention_mask,seqlen

class NN_DataHelper(DataHelper):
    # 切分词
    def on_data_process(self,data: typing.Any, user_data: tuple):
        tokenizer: BertTokenizer
        tokenizer, max_seq_length, do_lower_case, label2id, mode = user_data

        sentence1,sentence2,label_str = data
        labels = np.asarray(1 - label2id[label_str] if label_str is not None else 0, dtype=np.int64)

        input_ids, attention_mask, seqlen = pad_to_seqlength(sentence1, tokenizer, max_seq_length)
        input_ids_2, attention_mask_2, seqlen_2 = pad_to_seqlength(sentence2, tokenizer, max_seq_length)
        d = {
            'labels': labels,
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'seqlen': seqlen,
            'input_ids_2': input_ids_2,
            'attention_mask_2': attention_mask_2,
            'seqlen_2': seqlen_2
        }
        return d

    #读取标签
    def on_get_labels(self, files: typing.List[str]):
        D = ['0','1']
        label2id = {label: i for i, label in enumerate(D)}
        id2label = {i: label for i, label in enumerate(D)}
        return label2id, id2label

    # 读取文件
    def on_get_corpus(self, files: typing.List, mode:str):
        D = []
        for filename in files:
            with open(filename, mode='r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines:
                    jd = json.loads(line)
                    if not jd:
                        continue
                    D.append((jd['sentence1'],jd['sentence2'], jd.get('label',None)))
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

        seqlen = o.pop('seqlen_2')
        max_len = torch.max(seqlen)
        o['input_ids_2'] = o['input_ids_2'][:, :max_len]
        o['attention_mask_2'] = o['attention_mask_2'][:, :max_len]

        return o

class MyTransformer(TransformerModel, metaclass=TransformerMeta):
    def __init__(self,*args,**kwargs):
        super(MyTransformer, self).__init__(*args,**kwargs)
        config = self.config
        self.feat_head = nn.Linear(config.hidden_size, 512, bias=False)
        self.loss_fn = CoSentLoss()

    def get_model_lr(self):
        return super(MyTransformer, self).get_model_lr() + [
            (self.feat_head, self.config.task_specific_params['learning_rate_for_task'])
        ]

    def compute_loss(self,batch,batch_idx):
        labels: torch.Tensor = batch.pop('labels',None)
        if self.training:
            batch2 = {
                "input_ids": batch.pop('input_ids_2'),
                "attention_mask": batch.pop('attention_mask_2'),
            }
        logits1 = self.feat_head(self(**batch)[0][:, 0, :])
        if labels is not None:
            labels = labels.float()
            logits2 = self.feat_head(self(**batch2)[0][:, 0, :])

            mid_logits = torch.cat([logits1, logits2],dim=1)
            index = torch.arange(0, mid_logits.size(0))
            index = torch.cat([index[::2], index[1::2]])
            index = torch.unsqueeze(index, 1).expand(*mid_logits.size())
            mid_logits_state = torch.zeros_like(mid_logits)
            mid_logits_state = torch.scatter(mid_logits_state, dim=0, index=index, src=mid_logits)

            loss = self.loss_fn(mid_logits_state, labels)
            outputs = (loss,logits1,logits2)
        else:
            outputs = (logits1, )
        return outputs







if __name__== '__main__':
    parser = HfArgumentParser((ModelArguments, TrainingArguments,DataArguments))
    model_args, training_args, data_args = parser.parse_dict(train_info_args)

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
                dataHelper.make_dataset_with_args(data_args.train_file, token_fn_args_dict['train'], data_args,
                                       intermediate_name=intermediate_name, shuffle=True, mode='train'))
        if data_args.do_eval:
            eval_files.append(
                dataHelper.make_dataset_with_args(data_args.eval_file, token_fn_args_dict['eval'], data_args,
                                       intermediate_name=intermediate_name, shuffle=False, mode='eval'))
        if data_args.do_test:
            test_files.append(
                dataHelper.make_dataset_with_args(data_args.test_file, token_fn_args_dict['test'], data_args,
                                       intermediate_name=intermediate_name, shuffle=False, mode='test'))

    train_datasets = dataHelper.load_dataset(train_files, shuffle=True)
    eval_datasets = dataHelper.load_dataset(eval_files)
    test_datasets = dataHelper.load_dataset(test_files)
    if train_datasets:
        train_datasets = DataLoader(train_datasets, batch_size=training_args.train_batch_size,
                                    collate_fn=dataHelper.collate_fn,
                                    shuffle=False if isinstance(train_datasets, IterableDataset) else True)
    if eval_datasets:
        eval_datasets = DataLoader(eval_datasets, batch_size=training_args.eval_batch_size,
                                   collate_fn=dataHelper.collate_fn)
    if test_datasets:
        test_datasets = DataLoader(test_datasets, batch_size=training_args.test_batch_size,
                                   collate_fn=dataHelper.collate_fn)

    print('*' * 30, train_datasets, eval_datasets, test_datasets)

    model = MyTransformer(config=config, model_args=model_args, training_args=training_args)
    checkpoint_callback = ModelCheckpoint(monitor="val_loss", every_n_epochs=1)
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

    if train_datasets:
        trainer.fit(model, train_dataloaders=train_datasets,val_dataloaders=eval_datasets)

    if eval_datasets:
        trainer.validate(model, dataloaders=eval_datasets)

    if test_datasets:
        trainer.test(model, dataloaders=test_datasets)
