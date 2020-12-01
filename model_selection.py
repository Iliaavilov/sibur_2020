import pandas as pd
import neptune
import optuna
import neptunecontrib.monitoring.optuna as optuna_utils
import numpy as np
import copy
import lightgbm as lgb
import preprocessing
import os


class training:
    '''
    Класс, отвечающий за тренировку модели
    nn_model - модель, наследующая pl.LightningModule
    trainning_nn - класс, тренирующий nn_model
    остальные параметры - настройки эксперимента в neptune.ai
    '''
    def __init__(
            self, nn_model = None, training_nn = None,
            name=None, description=None, params = None,
            properties=None, tags=None, upload_source_files=None
    ):

        ## Создаем эксперимент в neptune
        neptune.create_experiment(name, description, params, properties, tags, upload_source_files)
        self.nn_model = nn_model
        self.training_nn = training_nn

    def set_up_studying(self, random_state, direction='minimize'):
        '''
        Инициализация подборки параметров в optuna
        random_state - random seed для сэмплера параметров
        direction - направление оптимизации (максимизация или минимизация)
        '''

        ## Выбираем стандартный сэмплер для подборки параметров
        sampler = optuna.samplers.TPESampler(seed=random_state)

        self.study = optuna.create_study(sampler=sampler, direction=direction)

    def train(self, X, y = None, cv=None, model=None, params_func=None, n_trials=None):
        '''
        X - фичи (может быть датафрейм/матрица, в зависимости от preprocessing)
        y - таргеты (может быть датафрейм/матрица, в зависимости от preprocessing)
        '''
        ## минимизируем ошибку
        self.study.optimize(lambda trial: self.objective(trial, X, y, cv, model, params_func),
                            n_trials=n_trials, callbacks=[optuna_utils.NeptuneCallback()])
    def lgbm_model(self,X, y, cv, params, log_importance, trial):
        '''
        X - фичи (матрица/датафрейм, не важно)
        y - таргеты (матрица/датафрейм, не важно)
        cv - список/вектор их фолдов(пример: [[[0, 1, 2], [3, 4, 5]]]) (как для sklearn gridsearch) (порядковые индексы)
        params - параметры для lgbm модели (и только для нее)
        '''
        def lgb_scoring(y_hat, data):
            '''
            Функция для оценивания качества модели на валидационной выборке
            Возвращает: название метрики, метрика, is_high_better
            '''
            y_true = data.get_label()
            return 'loss', np.mean(np.abs((y_true - y_hat)/y_true)), False

        train_data = lgb.Dataset(X, y)
        cv_model = lgb.cv(params = params,
                          train_set = train_data,
                          folds = cv[:-1],
                          feval = lgb_scoring,
                          early_stopping_rounds = 10,
                          verbose_eval = True)

        X_train = X.iloc[cv[-1][0], :]
        y_train = y.iloc[cv[-1][0]]
        X_test = X.iloc[cv[-1][1], :]
        y_test = y.iloc[cv[-1][1]]
        train_data = lgb.Dataset(X_train,
                                 y_train)
        test_data = lgb.Dataset(X_test,
                                y_test
                                )
        evals_result = {}
        params['n_estimators'] = len(cv_model['loss-mean'])
        test_model = lgb.train(params = params,
                               train_set = train_data,
                               valid_sets=[test_data],
                               valid_names=['test_data'],
                               feval = lgb_scoring,
                               evals_result = evals_result,
                               verbose_eval = False)
        if log_importance == True:
            feature_imp = pd.DataFrame({'Column': X.columns, 'Importance': test_model.feature_importance()})
            feature_imp.to_csv('feature_imp_{}.csv'.format(trial.number), index = False)
            neptune.log_artifact('feature_imp_{}.csv'.format(trial.number))
            os.remove('feature_imp_{}.csv'.format(trial.number))
        test_loss = evals_result['test_data']['loss'][-1]
        return (cv_model, test_loss)

    def pl_model(self, X, y, cv, params):
        '''
        X - фичи (матрица/датафрейм, не важно)
        y - таргеты (матрица/датафрейм, не важно)
        cv - список/вектор их фолдов(пример: [[[0, 1, 2], [3, 4, 5]]]) (как для sklearn gridsearch) (порядковые индексы)
        params - параметры для нейронки модели (и только для нее)
        batch_size -  размер батча
        '''


        batch_size = params['batch_size']
        params.pop('batch_size')
        iters = pd.DataFrame(columns=['B_C2H6', 'B_C3H8', 'B_iC4H10', 'B_nC4H10'])
        scores = pd.DataFrame(columns=['B_C2H6', 'B_C3H8', 'B_iC4H10', 'B_nC4H10'])
        fold_num = 0
        for fold in cv[:-1]:
            for target in ['B_C2H6', 'B_C3H8', 'B_iC4H10', 'B_nC4H10']:
                model_cv = self.training_nn(self.nn_model, X, y[[target]])
                model_cv.train(min_epochs=5,
                               max_epochs=3000,
                               model_params=params,
                               fold=fold,
                               batch_size=batch_size)
                trainer = model_cv.trainer
                iters.loc[fold_num, target] = float(trainer.current_epoch)
                scores.loc[fold_num, target] = float(trainer.callback_metrics['val_loss'].numpy())
            fold_num = fold_num+1
        best_iters = iters.mean(axis = 1).values
        best_cv = scores.mean(axis = 1).values

        test_loss = []
        for target in ['B_C2H6', 'B_C3H8', 'B_iC4H10', 'B_nC4H10']:

            mean_best_iter = round(iters[target].mean())
            model_test = self.training_nn(self.nn_model, X, y[[target]])
            test_losses = model_test.train(min_epochs=mean_best_iter,
                                           max_epochs=mean_best_iter,
                                           model_params=params,
                                           fold=cv[-1],
                                           val_fold=False,
                                           batch_size=batch_size)
            test_loss.append(float(model_cv.trainer.callback_metrics['val_loss'].numpy()))
        pp = 0
        for target in ['B_C2H6', 'B_C3H8', 'B_iC4H10', 'B_nC4H10']:
            neptune.log_text(target + '_cv_scores', str(list(scores[target].values)))
            neptune.log_metric(target + '_test_score', test_loss[pp])
            neptune.log_metric(target + '_run_score', np.mean(scores[target].values))
            neptune.log_metric(target + '_iters', np.mean(iters[target].values))
            pp = pp+1

        test_loss = np.mean(test_loss)

        return(np.mean(best_cv), np.std(best_cv), str(best_cv), np.mean(best_iters), test_loss)

    def objective(self, trial, X, y, cv, model, params_func):

        ## Множество параметров моделей
        params = params_func(trial, X)
        X_trans, y_trans, cv_trans, params_trans = preprocessing.preprocessing(X.copy(),
                                                                               y.copy(),
                                                                               copy.deepcopy(cv),
                                                                               copy.deepcopy(params))

        if model == 'lgbm':
            cv_model, test_loss = self.lgbm_model(X_trans,
                                                  y_trans,
                                                  cv_trans,
                                                  params_trans,
                                                  True,
                                                  trial)
            mean_cv = cv_model['loss-mean'][-1]
            iters = len(cv_model['loss-mean'])
            neptune.log_metric('std_cv_loss', cv_model['loss-stdv'][-1])
            neptune.log_metric('iterations', iters)
            neptune.log_metric('test_loss', test_loss)

        if model == 'torch':
            mean_cv, std_cv_loss, cv_scores, iterations, test_loss = self.pl_model(X_trans,
                                                                                   y_trans,
                                                                                   cv_trans,
                                                                                   params_trans)

            neptune.log_metric('std_cv_loss', std_cv_loss)
            neptune.log_text('cv_scores', cv_scores)
            neptune.log_metric('iterations', iterations)
            neptune.log_metric('test_loss', test_loss)

        return (mean_cv)