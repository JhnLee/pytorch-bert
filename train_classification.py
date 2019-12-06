from pytorch_transformers.modeling_bert import BertConfig
from pytorch_transformers import optimization

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from datasets import Datasets
from model import BertForEmotionClassification
from optim import layerwise_decay_optimizer

from sklearn.metrics import classification_report, confusion_matrix
from tqdm import tqdm
import time
import pickle
import config
import utils
import logging
import argparse
import random
import os
import warnings

warnings.filterwarnings('ignore')
logger = utils.get_logger('BERT Classification')
logger.setLevel(logging.INFO)


class ClassificationBatchFunction:
    # batch function for pytorch dataloader
    def __init__(self, max_len, pad_idx, cls_idx=None, sep_idx=None):
        self.max_len = max_len
        self.pad_idx = pad_idx
        self.cls_idx = cls_idx if cls_idx is not None else pad_idx
        self.sep_idx = sep_idx if sep_idx is not None else pad_idx

    def __call__(self, batch):
        tokens, label = list(zip(*batch))

        # Get max length from batch
        max_len = min(self.max_len, max([len(i) for i in tokens]))
        tokens = torch.tensor(
            [self.pad([self.cls_idx] + t + [self.sep_idx]) for t in tokens])
        masks = torch.ones_like(tokens).masked_fill(
            tokens == self.pad_idx, self.pad_idx)

        return tokens, masks, torch.tensor(label)

    def pad(self, sample):
        diff = self.max_len - len(sample)
        if diff > 0:
            sample += [self.pad_idx] * diff
        else:
            sample = sample[-self.max_len:]
        return sample


def train(args):
    # Set device
    if args.device == 'cuda':
        device = torch.device(
            'cuda') if torch.cuda.is_available() else torch.device('cpu')
        logger.info('use cuda')
    else:
        device = torch.device('cpu')
        logger.info('use cpu')
        
    # Set label list for classification
    if args.num_label == 'multi':
        label_list = ['공포', '놀람', '분노', '슬픔', '중립', '행복', '혐오']
    elif args.num_label == 'binary':
        label_list = ['긍정', '부정']
    logger.info('use {} labels for training'.format(len(label_list)))
    
    # Load pretrained model and model configuration
    pretrained_path = os.path.join('./pretrained_model/', args.pretrained_type)
    pretrained = torch.load(os.path.join(
        pretrained_path + '/pytorch_model.bin'))
    
    if args.pretrained_type == 'skt':
        # skt model의 파라미터 이름이 달라 수정
        new_keys_ = ['bert.' + k for k in pretrained.keys()]
        old_values_ = pretrained.values()
        pretrained = {k: v for k, v in zip(new_keys_, old_values_)}
        
    bert_config = BertConfig(os.path.join(
        pretrained_path + '/bert_config.json'))
    bert_config.num_labels = len(label_list)
    model = BertForEmotionClassification(bert_config).to(device)
    model.load_state_dict(pretrained, strict=False)

    # Load Datasets
    tr_set = Datasets(file_path=args.train_data_path,
                      label_list=label_list,
                      pretrained_type=args.pretrained_type,
                      objective='classification',
                      max_len=args.max_len)

    collate_fn = ClassificationBatchFunction(
        args.max_len, tr_set.pad_idx, tr_set.cls_idx, tr_set.sep_idx)
    tr_loader = DataLoader(dataset=tr_set,
                           batch_size=args.train_batch_size,
                           shuffle=True,
                           num_workers=8,
                           pin_memory=True,
                           drop_last=True,
                           collate_fn=collate_fn)

    dev_set = Datasets(file_path=args.dev_data_path,
                       label_list=label_list,
                       pretrained_type=args.pretrained_type,
                       objective='classification',
                       max_len=args.max_len)

    dev_loader = DataLoader(dataset=dev_set,
                            batch_size=args.eval_batch_size,
                            num_workers=8,
                            pin_memory=True,
                            drop_last=False,
                            collate_fn=collate_fn)

    # optimizer
    optimizer = layerwise_decay_optimizer(
        model=model, lr=args.learning_rate, layerwise_decay=args.layerwise_decay)

    # lr scheduler
    t_total = len(tr_loader) // args.gradient_accumulation_steps * args.epochs
    warmup_steps = int(t_total * args.warmup_percent)
    logger.info('total training steps : {}, lr warmup steps : {}'.format(t_total, warmup_steps))
    scheduler = optimization.WarmupLinearSchedule(
        optimizer, warmup_steps=warmup_steps, t_total=t_total)

    # for low-precision training
    if args.fp16:
        try:
            from apex import amp
            logger.info('Use fp16')
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(
            model, optimizer, opt_level=args.fp16_opt_level, verbosity=0)

    # tensorboard setting
    save_path = "./model_saved/lr {}, batch {}, len{}, warmup {}, len {}, {}, epoch {}".format(
        args.learning_rate, args.train_batch_size, args.warmup_percent, 
        args.gradient_accumulation_steps, args.max_len, args.pretrained_type, args.epochs)

    if not os.path.isdir(save_path):
        os.makedirs(save_path)
    writer = SummaryWriter(save_path)
    
    # Save best model results with resultwriter
    result_writer = utils.ResultWriter("./model_saved/results.csv")
    model.zero_grad()

    best_val_loss = 1e+9
    global_step = 0

    logger.info('***** Training starts *****')
    for epoch in tqdm(range(args.epochs), desc='epochs'):

        train_loss, train_acc, train_f1 = 0, 0, 0
        logging_loss, logging_acc, logging_f1 = 0, 0, 0

        for step, batch in tqdm(enumerate(tr_loader), desc='steps', total=len(tr_loader)):
            model.train()
            x_train, mask_train, y_train = map(lambda x: x.to(device), batch)

            inputs = {
                'input_ids': x_train,
                'attention_mask': mask_train,
                'classification_label': y_train,
            }

            output, loss = model(**inputs)
            y_max = output.max(dim=1)[1]
            
            cr = classification_report(y_train.tolist(),
                                            y_max.tolist(),
                                            target_names=label_list,
                                            output_dict=True)
            # Get accuracy / micro f1
            batch_acc = list(cr.items())[len(label_list)][1]
            # macro f1
            batch_macro_f1 = cr['macro avg']['f1-score']

            # accumulate measures
            grad_accu = args.gradient_accumulation_steps
            if grad_accu > 1:
                loss /= grad_accu
                batch_acc /= grad_accu
                batch_macro_f1 /= grad_accu

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            train_loss += loss.item()
            train_acc += batch_acc
            train_f1 += batch_macro_f1

            if (global_step + 1) % args.logging_step == 0:
                logging_acc = (train_acc - logging_acc) / args.logging_step
                logging_f1 = (train_f1 - logging_f1) / args.logging_step
                logging_loss = (train_loss - logging_loss) / args.logging_step
                
                logger.info('[{}/{}], trn loss : {:.3f}, trn acc : {:.3f}, macro f1 : {:.3f}, lr : {:.3f}'.format(
                    global_step + 1, t_total, logging_loss, logging_acc, logging_f1, scheduler.get_lr()[0]
                ))
                logging_acc, logging_f1, logging_loss = train_acc, train_f1, train_loss

                # Get f1 score for each label
                f1_results = [(l, r['f1-score']) for i, (l, r) in enumerate(cr.items()) if i < len(label_list)]
                f1_log = "\n".join(["{} : {}".format(l, f) for l, f in f1_results])
                logger.info("\n\n***f1-score***\n" + f1_log + "\n\n***confusion matrix***\n{}".format(
                    confusion_matrix(y_train.tolist(), y_max.tolist())))
                
            if (step + 1) % grad_accu == 0:
                if args.fp16:
                    torch.nn.utils.clip_grad_norm_(
                        amp.master_params(optimizer), args.grad_clip_norm)
                else:
                    torch.nn.utils.clip_grad_norm_(
                        model.parameters(), args.grad_clip_norm)

                scheduler.step()
                optimizer.step()
                model.zero_grad()
                global_step += 1

        train_loss /= (step + 1)
        train_acc /= (step+1)
        train_f1 /= (step+1)

        # Validation
        val_loss, val_acc, val_macro_f1 = evaluate(args, dev_loader, model, device)
        
        logger.info('[{}/{}] tr loss : {:.3f}, tr acc : {:.3f}. tr macro f1 : {:.3f}'.format(
            global_step + 1, t_total, train_loss, train_acc, train_f1
        ))
        logger.info('[{}/{}] val loss : {:.3f}, val acc : {:.3f}. val macro f1 : {:.3f}'.format(
            global_step + 1, t_total, val_loss, val_acc, val_macro_f1
        ))
        
        writer.add_scalars('loss', {'train': train_loss,
                                    'val': val_loss}, global_step + 1)
        writer.add_scalars('acc', {'train': train_acc,
                                   'val': val_acc}, global_step + 1)
        writer.add_scalars('macro_f1', {'train': train_f1,
                                   'val': val_macro_f1}, global_step + 1)
        
        if val_loss < best_val_loss:
            # Save model checkpoints
            torch.save(model.state_dict(), os.path.join(
                save_path, 'best_model.bin'))
            torch.save(args, os.path.join(save_path, 'training_args.bin'))
            logger.info('Saving model checkpoint to %s', save_path)
            best_val_loss = val_loss
            best_val_acc = val_acc

        train_loss, train_acc = 0, 0
        
    # Save results in 'model_saved/results.csv'
    results = {
            'val_loss' : best_val_loss,
            'val_acc' : best_val_acc,
        }
    result_writer.update(args, **results)
    return global_step, train_loss, train_acc, best_val_loss, best_val_acc

def evaluate(args, dataloader, model, device, objective='classification'):

    if args.num_label == 'multi':
        label_list = ['공포', '놀람', '분노', '슬픔', '중립', '행복', '혐오']
    elif args.num_label == 'binary':
        label_list = ['긍정', '부정']
        
    val_loss, val_acc, val_f1 = 0, 0, 0
    total_y = []
    total_y_hat = []

    for val_step, batch in enumerate(dataloader):
        model.eval()

        x_dev, mask_dev, y_dev = map(lambda x: x.to(device), batch)
        total_y += y_dev.tolist()

        inputs = {
            'input_ids': x_dev,
            'attention_mask': mask_dev,
            'classification_label': y_dev,
        }
        with torch.no_grad():
            output, loss = model(**inputs)
            y_max = output.max(dim=1)[1]
            total_y_hat += y_max.tolist()
        
            val_loss += loss.item()
            
    # f1-score 계산
    dev_cr = classification_report(total_y,
                                total_y_hat,
                                target_names=label_list,
                                output_dict=True)
    
    # Get accuracy / micro f1
    val_acc = list(dev_cr.items())[len(label_list)][1]
    # macro f1
    val_macro_f1 = dev_cr['macro avg']['f1-score']

    logger.info('***** Evaluation Results *****')
    f1_results = [(l, r['f1-score']) for i, (l, r) in enumerate(dev_cr.items()) if i < len(label_list)]
    f1_log = "\n".join(["{} : {}".format(l, f) for l, f in f1_results])
    logger.info("\n***f1-score***\n" + f1_log + "\n***confusion matrix***\n{}".format(
    confusion_matrix(total_y, total_y_hat)))

    val_loss /= (val_step + 1)

    return val_loss, val_acc, val_macro_f1


def set_seed(args):
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)


def main():
    parser = argparse.ArgumentParser()

    # Pretrained model Parameters
    parser.add_argument("--pretrained_type", default='skt', type=str,
                        help="type of pretrained model (skt, etri)")

    # Model Parmaeters

    # Train Parameters
    parser.add_argument("--train_batch_size", default=100, type=int,
                        help="batch size")
    parser.add_argument("--eval_batch_size", default=100, type=int,
                        help="batch size for validation")
    parser.add_argument("--layerwise_decay", action="store_true",
                        help="Whether to use layerwise decay")
    parser.add_argument("--learning_rate", default=2e-5, type=float,
                        help="The initial learning rate for Adam")
    parser.add_argument("--epochs", default=3, type=int,
                        help="total epochs")
    parser.add_argument("--gradient_accumulation_steps", default=1, type=int,
                        help="gradient accumulation steps for large batch training")
    parser.add_argument("--warmup_percent", default=0.01, type=float,
                        help="gradient warmup percentage")
    parser.add_argument("--grad_clip_norm", default=1.0, type=float,
                        help="batch size")

    # Other Parameters
    parser.add_argument("--logging_step", default=100, type=int,
                        help="logging step for training loss and acc")
    parser.add_argument("--device", default='cuda', type=str,
                        help="Whether to use cpu or cuda")
    parser.add_argument("--fp16", action="store_true",
                        help="Whether to use nvidia mixed precision training")
    parser.add_argument('--fp16_opt_level', type=str, default='O1',
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                        "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument("--seed", default=0, type=int,
                        help="Random seed(default=0)")

    # Data Parameters
    parser.add_argument("--train_data_path", default='./data/korean_single_train.csv', type=str,
                        help="train data path")
    parser.add_argument("--dev_data_path", default='./data/korean_single_dev.csv', type=str,
                        help="dev data path")
    parser.add_argument("--num_label", default='multi', type=str,
                        help="Number of labels in datastes(binary or multi)")
    parser.add_argument("--max_len", default=50, type=int,
                        help="Maximum sequence length")

    args = parser.parse_args()
    set_seed(args)

    t = time.time()
    global_step, train_loss, train_acc, best_val_loss, best_val_acc = train(args)
    elapsed = time.time() - t

    logger.info('***** Training done *****')
    logger.info('elapsed time: %.3f Hours' % (elapsed / 3600.))
    logger.info('best acc in test: %.4f' % best_val_acc)
    logger.info('best loss in test: %.4f' % best_val_loss)


if __name__ == '__main__':
    main()
